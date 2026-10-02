"""Playlist downloader: a yt-dlp wrapper ported from Lasso (Lasso/yt_download.py).

The flow is: save a playlist link -> fetch its entry list -> compare the titles
by name against the target folder -> let the owner tick which "new" videos to
download -> run yt-dlp on exactly those ids in a background thread.

Everything here is server-side and phone-friendly: there is no browser on the
server, so YouTube cookies are *pasted* into the UI (save_cookies) instead of
read with --cookies-from-browser like Lasso does.
"""
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from importlib import metadata
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.db import connection
from django.utils import timezone

from . import services
from .models import DownloadJob, DownloadSource, SkippedEntry, Video

logger = logging.getLogger(__name__)

_TEXT = services._SUBPROCESS_TEXT_KW
_NO_WINDOW = services._NO_WINDOW

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_URL_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
             "music.youtube.com", "youtu.be"}
_UNAVAILABLE_TITLES = {"[private video]", "[deleted video]", "[unavailable video]"}

# Per-entry statuses produced by classify(). Only NEW rows are pre-ticked.
NEW, DOWNLOADED, ARCHIVED, SKIPPED, UNAVAILABLE = (
    "new", "downloaded", "archived", "skipped", "unavailable")


# ---- Links, folders, names -------------------------------------------------

def normalize_url(raw):
    """Validate a pasted link and canonicalise playlist links.

    Only YouTube hosts are accepted. A watch?v=..&list=.. link is turned into
    the plain playlist URL: yt-dlp would otherwise treat it as the single
    video, and Lasso saves (and hashes for its archive file name) the plain
    form, so this keeps both tools pointed at the same archive."""
    u = (raw or "").strip()
    if not re.match(r"^https?://", u, re.I):
        raise ValueError("Paste a full https:// YouTube link")
    p = urlparse(u)
    if (p.hostname or "").lower() not in _YT_HOSTS:
        raise ValueError("Only YouTube links are supported")
    lst = parse_qs(p.query).get("list", [""])[0]
    if lst and re.fullmatch(r"[A-Za-z0-9_-]+", lst):
        return f"https://www.youtube.com/playlist?list={lst}"
    return u


def resolve_target(subdir):
    """Absolute download folder for a source: LIBRARY_ROOT + a relative
    subfolder (blank = the root itself). Raises ValueError if the subfolder
    would resolve outside LIBRARY_ROOT (.., absolute path, drive letter,
    symlink escape)."""
    root = os.path.normpath(settings.LIBRARY_ROOT)
    parts = [p for p in re.split(r"[\\/]+", (subdir or "").strip()) if p]
    if not parts:
        return root
    if any(p in (".", "..") for p in parts):
        raise ValueError("Folder must be inside the library")
    full = os.path.normpath(os.path.join(root, *parts))
    real_root, real_full = os.path.realpath(root), os.path.realpath(full)
    if real_full != real_root and not real_full.startswith(real_root + os.sep):
        raise ValueError("Folder must be inside the library")
    return full


def name_key(s):
    """Comparable form of a title or filename: NFKC, case-folded, and with
    everything that isn't a letter/digit removed. yt-dlp sanitises characters
    that are illegal in filenames (`/ : ? " | *` ...) differently across
    versions and platforms (dropped, replaced, or swapped for look-alike
    full-width characters), so the playlist title and the file name only line
    up once all punctuation, spaces and symbols are stripped from both."""
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return re.sub(r"[\W_]+", "", s)


def archive_path(source):
    """Lasso's archive file for this playlist: md5 of the saved URL, inside the
    target folder. Same name/format as Lasso so the two tools share it."""
    h = hashlib.md5(source.url.encode(), usedforsecurity=False).hexdigest()[:8]
    return os.path.join(resolve_target(source.target_subdir), f"_yt_archive_{h}.txt")


def read_archive_ids(path):
    ids = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    ids.add(parts[1])
    except OSError:
        pass
    return ids


def forget_in_archive(path, ids):
    """Drop `ids` from a yt-dlp archive file so a deliberate re-download isn't
    silently skipped (same idea as Lasso's forget_video). Only edits the text
    file; never touches media."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    kept = [l for l in lines if not (len(l.split()) >= 2 and l.split()[1] in ids)]
    if len(kept) != len(lines):
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(kept)


def local_index(source):
    """What's already on disk for this source: {'names': set of name_keys,
    'ids': set of video ids, 'archive': set of ids in Lasso's archive file}.

    The walk is recursive because Organize moves files into YYYY-MM/DD/
    folders. Library rows under the folder add their yt-dlp sidecar title
    (the real, unsanitised title) and source URL (the video id)."""
    target = resolve_target(source.target_subdir)
    names, ids = set(), set()
    if os.path.isdir(target):
        for dirpath, _dirs, files in os.walk(target):
            for f in files:
                stem, ext = os.path.splitext(f)
                if ext.lower() in settings.VIDEO_EXTENSIONS:
                    k = name_key(stem)
                    if k:
                        names.add(k)
    prefix = target.rstrip(os.sep) + os.sep
    rows = Video.objects.filter(file_path__startswith=prefix, missing=False)
    for title, url in rows.values_list("title", "source_url").iterator():
        k = name_key(title)
        if k:
            names.add(k)
        m = _URL_ID_RE.search(url or "")
        if m:
            ids.add(m.group(1))
    return {"names": names, "ids": ids, "archive": read_archive_ids(archive_path(source))}


def classify(entries, index, skipped_ids):
    """Attach a `status` to every playlist entry (a new list of new dicts).
    Precedence: unavailable > downloaded (name or id matches a local file) >
    skipped (owner marked it) > archived (only in Lasso's archive) > new."""
    out = []
    for e in entries:
        vid, title = e["id"], e.get("title") or ""
        key = name_key(title)
        if title.strip().lower() in _UNAVAILABLE_TITLES:
            status = UNAVAILABLE
        elif vid in index["ids"] or (key and key in index["names"]):
            status = DOWNLOADED
        elif vid in skipped_ids:
            status = SKIPPED
        elif vid in index["archive"]:
            status = ARCHIVED
        else:
            status = NEW
        out.append({**e, "status": status})
    return out


def source_state(source):
    """Cached entries re-classified against the current disk/skip state. Cheap
    enough to call after every skip/unskip/job; only fetch_playlist() hits
    YouTube."""
    try:
        entries = json.loads(source.entries_json or "[]")
    except ValueError:
        entries = []
    skipped = set(source.skipped.values_list("video_id", flat=True))
    return classify(entries, local_index(source), skipped)


# ---- Cookies ---------------------------------------------------------------
# The pasted cookies carry the owner's YouTube login, so they are stored
# 0600 outside LIBRARY_ROOT and never sent back to the browser -- the UI only
# ever learns "set / how many / when".

_LOGIN_COOKIES = {"SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO"}
_FAR_FUTURE = "2147483647"


def cookie_path():
    return os.path.join(settings.YTDL_DIR, "cookies.txt")


def normalize_cookies(text):
    """Accept a Netscape cookies.txt paste *or* a raw `Cookie:` request header
    (`a=b; c=d`, copied from DevTools) and return (netscape_text, count,
    has_login_cookie). Raises ValueError if nothing usable is found.

    Netscape lines are matched on any whitespace, not just tabs, because phone
    keyboards/paste boxes often turn tabs into spaces."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Nothing pasted")
    rows = []  # (domain, flag, path, secure, expiry, name, value)
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        f = re.split(r"\s+", line, maxsplit=6)
        if (len(f) >= 6 and f[1].upper() in ("TRUE", "FALSE")
                and f[3].upper() in ("TRUE", "FALSE") and re.fullmatch(r"\d+", f[4])):
            rows.append((f[0], f[1].upper(), f[2], f[3].upper(), f[4], f[5],
                         f[6] if len(f) > 6 else ""))
    if not rows:  # not Netscape -> try a raw Cookie header
        # If a whole block of request headers was pasted, take just the Cookie line.
        m = re.search(r"^\s*cookie\s*:\s*(.+)$", text, re.I | re.M)
        header = m.group(1) if m else " ".join(text.split("\n"))
        for part in header.split(";"):
            name, sep, value = part.strip().partition("=")
            name, value = name.strip(), value.strip()
            if sep and name and re.fullmatch(r"[^\s=;,\"\\]+", name):
                rows.append((".youtube.com", "TRUE", "/", "TRUE", _FAR_FUTURE, name, value))
    if not rows:
        raise ValueError("Couldn't find any cookies in that. Paste the whole cookies.txt "
                         "file, or the Cookie: header value.")
    lines = ["# Netscape HTTP Cookie File"]
    for r in rows:
        if "\t" in r[6]:   # would corrupt the tab-separated layout
            continue
        lines.append("\t".join(r))
    count = len(lines) - 1
    if count == 0:
        raise ValueError("Those cookies were malformed")
    has_login = any(r[5] in _LOGIN_COOKIES for r in rows)
    return "\n".join(lines) + "\n", count, has_login


def save_cookies(text):
    """Validate + store pasted cookies. Returns (count, has_login_cookie)."""
    body, count, has_login = normalize_cookies(text)
    path = cookie_path()
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    os.replace(tmp, path)
    return count, has_login


def clear_cookies():
    try:
        os.remove(cookie_path())
    except OSError:
        pass


def cookie_status():
    path = cookie_path()
    if not os.path.isfile(path):
        return {"set": False}
    try:
        with open(path, encoding="utf-8") as f:
            rows = [l for l in f if l.strip() and not l.startswith("# ")]
        names = {l.split("\t")[5] for l in rows if len(l.split("\t")) >= 6}
        return {"set": True, "count": len(rows),
                "has_login": bool(names & _LOGIN_COOKIES),
                "saved": os.path.getmtime(path)}
    except OSError:
        return {"set": False}


# ---- yt-dlp availability / command building --------------------------------

def ytdlp_available():
    return importlib.util.find_spec("yt_dlp") is not None


def ytdlp_version():
    try:
        return metadata.version("yt-dlp")
    except metadata.PackageNotFoundError:
        return ""


def js_runtime():
    """(name, args) like Lasso's check_runtime_silent(): deno is picked up by
    yt-dlp with no flag, node needs an explicit --js-runtimes. yt-dlp needs
    one of them to solve YouTube's n-challenge (without it every real format
    is missing)."""
    if shutil.which("deno"):
        return "deno", []
    node = shutil.which("node")
    if node:
        return "node", ["--js-runtimes", f"node:{node}"]
    return "", []


def _common_args(cookie_file):
    _name, rt_args = js_runtime()
    cmd = [sys.executable, "-m", "yt_dlp", *rt_args]
    if cookie_file:
        cmd += ["--cookies", cookie_file]
        # With cookies attached YouTube forces the default clients into SABR
        # streaming (muxed HLS only, ~1080p max). web_embedded keeps the full
        # DASH ladder; `default` is the fallback for videos with embedding
        # disabled. See Lasso/CLAUDE.md and yt-dlp issue #12482.
        player_client = "web_embedded,default"
    else:
        player_client = "default,-android_sdkless"
    cmd += ["--extractor-args", f"youtube:player_client={player_client}",
            # Fetches yt-dlp's n-challenge solver script; the JS runtime alone
            # isn't enough.
            "--remote-components", "ejs:github"]
    return cmd


def _env():
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def build_cmd(ids, out_dir, archive, cookie_file):
    """Port of Lasso's build_cmd() for a hand-picked list of ids. Returns
    (cmd, before_file, after_file, batch_file).

    before/after are --print-to-file markers (one id per line attempted / fully
    finished) -- yt-dlp's exit code is not a reliable success signal with
    --ignore-errors, so the real counts come from diffing these. They live in
    YTDL_DIR (not the library, unlike Lasso) so they can never be scanned or
    left behind next to the videos. Videos are passed through a batch file so
    a long selection can't hit the OS command-line length limit."""
    run_id = uuid.uuid4().hex[:8]
    before = os.path.join(settings.YTDL_DIR, f"run_{run_id}_attempt.txt")
    after = os.path.join(settings.YTDL_DIR, f"run_{run_id}_success.txt")
    batch = os.path.join(settings.YTDL_DIR, f"run_{run_id}_urls.txt")
    with open(batch, "w", encoding="utf-8") as f:
        f.write("\n".join(f"https://www.youtube.com/watch?v={i}" for i in ids) + "\n")

    cmd = _common_args(cookie_file)
    cmd += ["-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                  "/bestvideo[ext=mp4]+bestaudio"
                  "/bestvideo+bestaudio/best",
            "--merge-output-format", "mp4",
            "--write-thumbnail", "--convert-thumbnails", "jpg",
            # Sidecar read by services.read_sidecar(): real title/channel/url/date.
            "--write-info-json",
            "--download-archive", archive,
            "-o", os.path.join(out_dir, "%(title)s.%(ext)s"),
            "--retries", "6", "--fragment-retries", "10",
            "--concurrent-fragments", "4", "--ignore-errors", "--geo-bypass",
            "--newline",
            "--progress-template",
            "download:HFP|%(info.id)s|%(progress._percent_str)s|%(info.title)s",
            "--print-to-file", "before_dl:%(id)s", before,
            "--print-to-file", "after_move:%(id)s", after,
            "-a", batch]
    return cmd, before, after, batch


def _read_ids(path):
    """Read + delete a marker file; a missing file (nothing reached that
    stage, or yt-dlp crashed first) is just an empty set."""
    try:
        with open(path, encoding="utf-8") as f:
            ids = {l.strip() for l in f if l.strip()}
    except OSError:
        ids = set()
    try:
        os.remove(path)
    except OSError:
        pass
    return ids


def _count_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for l in f if l.strip())
    except OSError:
        return 0


def hint_for(text):
    """Turn a yt-dlp error into advice the UI can show."""
    t = (text or "").lower()
    if "not a bot" in t or "sign in" in t or ("cookies" in t and "invalid" in t):
        return "YouTube wants a sign-in. Paste fresh cookies above."
    if "private" in t or "members-only" in t or "join this channel" in t:
        return "Private or members-only. Cookies from an account that can see it are needed."
    if "requested format is not available" in t or "challenge" in t:
        return "yt-dlp can't solve YouTube's challenge. Update yt-dlp and check Deno/Node is installed."
    return ""


# ---- Fetching a playlist ---------------------------------------------------

def _flatten(node, out, seen):
    for e in node.get("entries") or []:
        if not e:
            continue
        if e.get("entries"):           # a channel's tabs: recurse
            _flatten(e, out, seen)
            continue
        vid = e.get("id") or ""
        if not VIDEO_ID_RE.match(vid) or vid in seen:
            continue                   # nested-playlist stubs have longer ids
        seen.add(vid)
        dur = e.get("duration")
        out.append({"id": vid, "title": e.get("title") or vid,
                    "duration": int(dur) if isinstance(dur, (int, float)) else None,
                    "index": len(out) + 1})


# Fetch runs inside a web request, and the documented gunicorn unit uses
# --timeout 120: past that the worker is killed and the client just sees a
# 502. Stay under it so a slow playlist gets a clean "try again" message.
FETCH_TIMEOUT = 100


def fetch_playlist(url, timeout=FETCH_TIMEOUT):
    """Ask yt-dlp for the playlist's entries without downloading anything
    (--flat-playlist). Returns (name, entries, error); error is "" on success.
    Runs on a *copy* of the cookie file so yt-dlp's cookie write-back can't
    race a running download that is using the real one."""
    if not ytdlp_available():
        return "", [], "yt-dlp isn't installed on the server (pip install yt-dlp)."
    tmp = None
    try:
        cookie = None
        if os.path.isfile(cookie_path()):
            fd, tmp = tempfile.mkstemp(prefix="fetch_", suffix=".txt", dir=settings.YTDL_DIR)
            os.close(fd)
            shutil.copyfile(cookie_path(), tmp)
            cookie = tmp
        # Listing only: no formats are resolved, so none of the download-time
        # extractor args / challenge solver are needed -- just the cookies.
        cmd = [sys.executable, "-m", "yt_dlp"]
        if cookie:
            cmd += ["--cookies", cookie]
        cmd += ["--flat-playlist", "--dump-single-json", "--ignore-errors",
                "--no-warnings", url]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=_env(),
                               creationflags=_NO_WINDOW, **_TEXT)
        except subprocess.TimeoutExpired:
            return "", [], "YouTube took too long to answer. Try again."
        except OSError as e:
            return "", [], f"Couldn't run yt-dlp: {e}"
        try:
            data = json.loads(r.stdout)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            errs = [l for l in (r.stderr or "").splitlines() if l.startswith("ERROR")]
            msg = (errs[-1] if errs else (r.stderr or "").strip()[-300:] or "No data returned")
            hint = hint_for(r.stderr)
            return "", [], f"{msg[:300]} {hint}".strip()
        entries, seen = [], set()
        if data.get("entries") is not None:
            _flatten(data, entries, seen)
        elif VIDEO_ID_RE.match(data.get("id") or ""):     # a single-video link
            _flatten({"entries": [data]}, entries, seen)
        return data.get("title") or "", entries, ""
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---- Running a download job ------------------------------------------------

def _kill_tree(pid):
    """Stop yt-dlp and the ffmpeg it spawned for merging."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, creationflags=_NO_WINDOW)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        services._kill_pid(pid)


def reap_stale():
    """Fail jobs whose yt-dlp is gone without the owning thread having
    finished them (server restarted mid-download). Conditional UPDATEs so a
    thread that finishes at the same instant is never overwritten."""
    now = timezone.now()
    for job in DownloadJob.objects.filter(status__in=[DownloadJob.QUEUED, DownloadJob.RUNNING]):
        if job.pid:
            alive = services._pid_alive(job.pid)
        else:                      # thread hasn't spawned yet
            alive = (now - job.started).total_seconds() < 60
        if not alive:
            DownloadJob.objects.filter(
                pk=job.pk, status__in=[DownloadJob.QUEUED, DownloadJob.RUNNING]
            ).update(status=DownloadJob.FAILED, finished=now,
                     summary="Interrupted (server restarted?)")


def active_job():
    reap_stale()
    return DownloadJob.objects.filter(
        status__in=[DownloadJob.QUEUED, DownloadJob.RUNNING]).first()


def start_job(source, ids):
    """Create a job for `ids` and run it in a background thread. Returns
    (job, error). One download at a time."""
    if not ytdlp_available():
        return None, "yt-dlp isn't installed on the server (pip install yt-dlp)."
    if active_job():
        return None, "A download is already running."
    try:
        resolve_target(source.target_subdir)
    except ValueError as e:
        return None, str(e)
    job = DownloadJob.objects.create(source=source, ids=json.dumps(ids), total=len(ids),
                                     status=DownloadJob.QUEUED)
    threading.Thread(target=_run_job, args=(job.pk, True), daemon=True).start()
    return job, ""


def cancel_job():
    job = active_job()
    if not job:
        return False
    DownloadJob.objects.filter(pk=job.pk).update(
        status=DownloadJob.CANCELLED, finished=timezone.now(), summary="Cancelled")
    if job.pid:
        _kill_tree(job.pid)
    return True


def _run_job(job_id, close_db=False):
    """Thread body. `close_db` is only True for real threads: a thread's own
    SQLite connection must be closed when it ends, but the tests call this
    directly inside their transaction, where closing would break them."""
    try:
        _run_job_inner(job_id)
    except Exception as e:  # never leave a job stuck on "running"
        logger.exception("download job %s crashed", job_id)
        DownloadJob.objects.filter(
            pk=job_id, status__in=[DownloadJob.QUEUED, DownloadJob.RUNNING]
        ).update(status=DownloadJob.FAILED, finished=timezone.now(), summary=f"Crashed: {e}"[:250])
    finally:
        if close_db:
            connection.close()


def _run_job_inner(job_id):
    job = DownloadJob.objects.select_related("source").get(pk=job_id)
    source = job.source
    ids = json.loads(job.ids)
    out_dir = resolve_target(source.target_subdir)
    os.makedirs(out_dir, exist_ok=True)

    # The owner explicitly picked these, so they must not be skipped as
    # already-archived (that's the point of re-selecting an "archived" row).
    archive = archive_path(source)
    forget_in_archive(archive, set(ids))

    cookie = cookie_path() if os.path.isfile(cookie_path()) else None
    cmd, before, after, batch = build_cmd(ids, out_dir, archive, cookie)

    log, last_flush = [], 0.0
    cur_title, cur_pct = "", 0

    def flush(force=False):
        nonlocal last_flush
        now = time.time()
        if not force and now - last_flush < 1.0:
            return
        last_flush = now
        DownloadJob.objects.filter(pk=job_id, status=DownloadJob.RUNNING).update(
            current_title=cur_title[:500], current_pct=cur_pct,
            done_count=_count_lines(after), log_tail="\n".join(log[-40:]))

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=_env(), creationflags=_NO_WINDOW,
                                start_new_session=(os.name != "nt"), **_TEXT)
    except OSError as e:
        DownloadJob.objects.filter(pk=job_id).update(
            status=DownloadJob.FAILED, finished=timezone.now(), summary=f"Couldn't start yt-dlp: {e}"[:250])
        return
    # Don't resurrect a job the owner cancelled in the instant before spawn.
    DownloadJob.objects.filter(pk=job_id, status=DownloadJob.QUEUED).update(
        status=DownloadJob.RUNNING, pid=proc.pid)
    if DownloadJob.objects.filter(pk=job_id, status=DownloadJob.CANCELLED).exists():
        _kill_tree(proc.pid)

    for line in proc.stdout:
        line = line.rstrip()
        if line.startswith("HFP|"):
            parts = line.split("|", 3)
            if len(parts) == 4:
                try:
                    cur_pct = int(float(re.sub(r"[^\d.]", "", parts[2]) or 0))
                except ValueError:
                    pass
                cur_title = parts[3]
        elif line:
            log.append(line[:300])
        flush()
    proc.wait()

    _read_ids(before)               # only to delete it; success is judged by `after`
    succeeded = _read_ids(after)
    try:
        os.remove(batch)
    except OSError:
        pass
    ok = len(succeeded & set(ids))
    fail = len(ids) - ok            # unavailable items never reach the "attempted" stage
    tail = "\n".join(log[-40:])
    if fail == 0:
        status, summary = DownloadJob.DONE, f"{ok} downloaded"
    elif ok == 0:
        status = DownloadJob.FAILED
        summary = ("Nothing downloaded. " + hint_for(tail)).strip()
    else:
        status, summary = DownloadJob.DONE, f"{ok} downloaded, {fail} failed or unavailable"

    # A cancel (view already set status + summary) keeps its status; only the
    # counts and log are filled in.
    cancelled = DownloadJob.objects.filter(pk=job_id, status=DownloadJob.CANCELLED).exists()
    fields = dict(done_count=ok, fail_count=fail, current_pct=0, current_title="",
                  pid=None, log_tail=tail, finished=timezone.now())
    if not cancelled:
        fields.update(status=status, summary=summary[:250])
    DownloadJob.objects.filter(pk=job_id).update(**fields)

    # Last, so the job already reads "done" while thumbnails are generated.
    if ok:
        try:
            services.scan_library()
        except Exception:
            logger.exception("post-download scan failed")


# ---- Updating yt-dlp -------------------------------------------------------

def _update_file():
    return os.path.join(settings.YTDL_DIR, "update.json")


def update_status():
    try:
        with open(_update_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"state": "idle"}


def start_update():
    """pip-upgrade yt-dlp in the background (YouTube breaks it regularly).
    Progress goes to a file, not memory, so any gunicorn worker can report it."""
    if update_status().get("state") == "running":
        return False

    def write(state, msg=""):
        with open(_update_file(), "w", encoding="utf-8") as f:
            json.dump({"state": state, "msg": msg, "at": time.time()}, f)

    def run():
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp[default]", "yt-dlp-ejs"],
                capture_output=True, timeout=600, creationflags=_NO_WINDOW, **_TEXT)
            if r.returncode == 0:
                write("ok", f"yt-dlp {ytdlp_version()}")
            else:
                write("error", (r.stderr or r.stdout or "pip failed").strip()[-300:])
        except Exception as e:
            write("error", str(e)[:300])

    write("running")
    threading.Thread(target=run, daemon=True).start()
    return True
