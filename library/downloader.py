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
from django.db.models import Count, Q, Sum
from django.utils import timezone

from . import roots, services
from .models import DownloadJob, DownloadSource, SkippedEntry, Video

logger = logging.getLogger(__name__)

_TEXT = services._SUBPROCESS_TEXT_KW
_NO_WINDOW = services._NO_WINDOW

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_URL_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")
URL_ID_RE = _URL_ID_RE
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


def resolve_target(subdir, root=None):
    """Absolute download folder for a source: a library folder (default: the
    main one, where downloads are saved) + a relative subfolder (blank = the
    folder itself). Raises ValueError if the subfolder would resolve outside
    that folder (.., absolute path, drive letter, symlink escape)."""
    root = os.path.normpath(root or roots.main_root())
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


def all_targets(subdir):
    """The source's subfolder in every library folder (the main one first):
    what counts as "already downloaded" is looked up in all of them, since a
    playlist's videos may have been saved before the main folder was switched."""
    main = roots.main_root()
    out = []
    for r in [main] + [r for r in roots.library_roots() if r != main]:
        try:
            out.append(resolve_target(subdir, r))
        except ValueError:
            continue
    return out


def name_key(s):
    """Comparable form of a title or filename: NFKC, case-folded, and with
    everything that isn't a letter/digit removed. yt-dlp sanitises characters
    that are illegal in filenames (`/ : ? " | *` ...) differently across
    versions and platforms (dropped, replaced, or swapped for look-alike
    full-width characters), so the playlist title and the file name only line
    up once all punctuation, spaces and symbols are stripped from both."""
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return re.sub(r"[\W_]+", "", s)


def _archive_name(source):
    h = hashlib.md5(source.url.encode(), usedforsecurity=False).hexdigest()[:8]
    return f"_yt_archive_{h}.txt"


def archive_path(source):
    """Lasso's archive file for this playlist: md5 of the saved URL, inside the
    main download folder. Same name/format as Lasso so the two tools share it."""
    return os.path.join(resolve_target(source.target_subdir), _archive_name(source))


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

    Looked up in the playlist's subfolder of *every* library folder. The walk
    is recursive because Organize moves files into YYYY-MM/DD/ folders.
    Library rows under those folders add their yt-dlp sidecar title (the real,
    unsanitised title) and source URL (the video id)."""
    targets = all_targets(source.target_subdir)
    names, ids, archived = set(), set(), set()
    for target in targets:
        if os.path.isdir(target):
            for dirpath, _dirs, files in os.walk(target):
                for f in files:
                    stem, ext = os.path.splitext(f)
                    if ext.lower() in settings.VIDEO_EXTENSIONS:
                        k = name_key(stem)
                        if k:
                            names.add(k)
        archived |= read_archive_ids(os.path.join(target, _archive_name(source)))
    if targets:
        q = Q()
        for t in targets:
            q |= Q(file_path__startswith=roots.prefix(t))
        rows = Video.objects.filter(q, missing=False)
        for title, url in rows.values_list("title", "source_url").iterator():
            k = name_key(title)
            if k:
                names.add(k)
            m = _URL_ID_RE.search(url or "")
            if m:
                ids.add(m.group(1))
    return {"names": names, "ids": ids, "archive": archived}


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


# ---- Disk space ------------------------------------------------------------
# Rough size estimate = playlist duration x a bytes-per-second rate. The rate
# is calibrated on the user's own library (what their downloads actually
# weigh), with a default for an empty one. It's only a guide: the real size
# depends on the formats YouTube serves.
DEFAULT_RATE = 600_000                 # ~4.8 Mbit/s: typical 1080p H.264 + AAC
_RATE_BOUNDS = (150_000, 4_000_000)
MIN_FREE_BYTES = 1024 ** 3             # refuse to start below this


def estimate_rate():
    """(bytes_per_second, "library"|"default")."""
    agg = Video.objects.filter(missing=False, duration_seconds__gt=0, size_bytes__gt=0).aggregate(
        n=Count("id"), size=Sum("size_bytes"), dur=Sum("duration_seconds"))
    if (agg["n"] or 0) >= 3 and agg["dur"]:
        rate = agg["size"] / agg["dur"]
        return int(min(max(rate, _RATE_BOUNDS[0]), _RATE_BOUNDS[1])), "library"
    return DEFAULT_RATE, "default"


def free_space(path):
    """(free_bytes, total_bytes) of the disk `path` will live on -- the target
    folder may not exist yet, so ask its nearest existing parent. (None, None)
    if it can't be determined."""
    p = path
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        u = shutil.disk_usage(p)
        return u.free, u.total
    except OSError:
        return None, None


def disk_info(source):
    free, total = free_space(resolve_target(source.target_subdir))
    rate, src = estimate_rate()
    return {"free": free, "total": total, "rate": rate, "rate_source": src}


# ---- Cookies ---------------------------------------------------------------
# The pasted cookies carry the owner's YouTube login, so they are stored
# 0600 outside the library and never sent back to the browser -- the UI only
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


def _cookie_rows():
    """[(name, expiry_epoch_or_0)] for every stored cookie, or None."""
    try:
        with open(cookie_path(), encoding="utf-8") as f:
            rows = [l.rstrip("\n").split("\t") for l in f if l.strip() and not l.startswith("# ")]
    except OSError:
        return None
    out = []
    for r in rows:
        if len(r) >= 6:
            try:
                exp = int(r[4])
            except ValueError:
                exp = 0
            out.append((r[5], exp))
    return out


def cookie_status():
    path = cookie_path()
    rows = _cookie_rows()
    if rows is None:
        return {"set": False}
    try:
        saved = os.path.getmtime(path)
    except OSError:
        return {"set": False}
    names = {n for n, _ in rows}
    # When the login actually lapses: the earliest *real* expiry among the login
    # cookies (0 = session cookie, and a pasted Cookie: header gets a far-future
    # placeholder -- neither says anything).
    exps = [e for n, e in rows if n in _LOGIN_COOKIES and 0 < e < int(_FAR_FUTURE)]
    login_expires = min(exps) if exps else None
    chk = read_cookie_check()
    return {"set": True, "count": len(rows), "has_login": bool(names & _LOGIN_COOKIES),
            "saved": saved, "login_expires": login_expires,
            "expired": bool(login_expires and login_expires < time.time()),
            "check": chk}


# ---- Do the saved cookies still work? ---------------------------------------
# YouTube invalidates exported cookies without telling anyone (and yt-dlp then
# carries on *anonymously* with just a warning), so the only real test is to ask
# for something that needs a signed-in account: the Watch Later list.

COOKIE_BAD = ("Your YouTube cookies don't work anymore (expired or signed out). "
              "Paste fresh ones in the cookies box.")
CHECK_FRESH = 15 * 60


def _check_file():
    return os.path.join(settings.YTDL_DIR, "cookie_check.json")


def read_cookie_check():
    """The last test result for the *current* cookie file, else None."""
    try:
        with open(_check_file(), encoding="utf-8") as f:
            c = json.load(f)
        if abs(c.get("cookie_mtime", 0) - os.path.getmtime(cookie_path())) > 1e-3:
            return None
        return c
    except (OSError, ValueError):
        return None


def _write_check(ok, msg):
    try:
        mt = os.path.getmtime(cookie_path())
    except OSError:
        return None
    c = {"ok": ok, "msg": msg, "checked": time.time(), "cookie_mtime": mt}
    tmp = _check_file() + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(c, f)
        os.replace(tmp, _check_file())
    except OSError:
        pass
    return c


def check_cookies(timeout=60):
    """Run the Watch Later probe on a *copy* of the cookies and store/return
    {ok, msg, ...}. ok is True (signed in), False (cookies dead) or None
    (couldn't tell -- network down, yt-dlp missing); None never blocks."""
    if not os.path.isfile(cookie_path()):
        return {"ok": False, "msg": "No cookies saved", "checked": time.time()}
    if not ytdlp_available():
        return _write_check(None, "yt-dlp isn't installed")
    fd, tmp = tempfile.mkstemp(prefix="check_", suffix=".txt", dir=settings.YTDL_DIR)
    os.close(fd)
    try:
        shutil.copyfile(cookie_path(), tmp)
        cmd = [sys.executable, "-m", "yt_dlp", "--cookies", tmp, "--flat-playlist",
               "--dump-single-json", "--playlist-end", "1", "--no-warnings",
               "https://www.youtube.com/playlist?list=WL"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=_env(),
                               creationflags=_NO_WINDOW, **_TEXT)
        except subprocess.TimeoutExpired:
            return _write_check(None, "YouTube took too long to answer")
        except OSError as e:
            return _write_check(None, f"Couldn't run yt-dlp: {e}")
        err = (r.stderr or "").lower()
        if "no longer valid" in err or "sign in" in err or "log in" in err or "login" in err:
            return _write_check(False, COOKIE_BAD)
        if r.returncode == 0:
            try:
                if isinstance(json.loads(r.stdout), dict):
                    return _write_check(True, "Signed in")
            except ValueError:
                pass
        if any(w in err for w in ("playlist does not exist", "unviewable", "private", "not available",
                                  "does not exist")):
            return _write_check(False, COOKIE_BAD)       # WL is always visible to its owner
        return _write_check(None, "Couldn't tell (" + (err.strip().splitlines() or ["no answer"])[-1][:120] + ")")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def check_cookies_async():
    def go():
        try:
            check_cookies()
        finally:
            connection.close()
    threading.Thread(target=go, daemon=True).start()


def cookies_ok_for_job():
    """None if downloading may go ahead, else the message to refuse with.
    Reuses a passing result for a few minutes so back-to-back jobs don't each
    pay for a probe."""
    if not os.path.isfile(cookie_path()):
        return "YouTube cookies are required. Paste them first."
    st = cookie_status()
    if st.get("expired"):
        return COOKIE_BAD
    c = st.get("check")
    if not c or time.time() - c.get("checked", 0) > CHECK_FRESH or c.get("ok") is None:
        c = check_cookies()
    return COOKIE_BAD if c and c.get("ok") is False else None


# ---- yt-dlp availability / command building --------------------------------

def ytdlp_available():
    return importlib.util.find_spec("yt_dlp") is not None


def ytdlp_version():
    try:
        return metadata.version("yt-dlp")
    except metadata.PackageNotFoundError:
        return ""


# Oldest runtime versions yt-dlp's challenge solver accepts -- mirrors
# MIN_SUPPORTED_VERSION in yt_dlp/utils/_jsruntime.py (and install.sh). A
# runtime below these is silently rejected by yt-dlp: the n-challenge isn't
# solved, formats go missing and every download dies with "The page needs to
# be reloaded" -- exactly what an old distro Node 18 does.
_MIN_RUNTIME = {"deno": (2, 3, 0), "bun": (1, 2, 11), "node": (22, 0, 0)}
_VERSION_CACHE = {}


def _exe_version(path):
    """(major, minor, patch) from `<path> --version`, or None. Cached per
    (path, mtime) since the tools endpoint and every job ask for it."""
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    if key in _VERSION_CACHE:
        return _VERSION_CACHE[key]
    try:
        r = subprocess.run([path, "--version"], capture_output=True, timeout=15,
                           creationflags=_NO_WINDOW, **_TEXT)
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", r.stdout or "")
        ver = tuple(int(x) for x in m.groups()) if m else None
    except (OSError, subprocess.SubprocessError):
        ver = None
    _VERSION_CACHE[key] = ver
    return ver


def local_deno_path():
    """Deno installed by install.sh into the project, so the service needs no
    PATH entry for it."""
    exe = "deno.exe" if os.name == "nt" else "deno"
    return os.path.join(str(settings.BASE_DIR), ".deno", "bin", exe)


def js_runtime():
    """Best JS runtime for yt-dlp as {name, path, version, supported, args};
    `args` is the explicit --js-runtimes flag (empty unless supported).
    Prefers Deno, then Bun, then Node, and takes the first one that is new
    enough. If only too-old ones exist, the best of those is returned with
    supported=False so the UI can say *which* version is the problem."""
    cands = []
    for name in ("deno", "bun", "node"):
        found = shutil.which(name)
        if found:
            cands.append((name, found))
        if name == "deno" and os.path.isfile(local_deno_path()):
            cands.append(("deno", local_deno_path()))
    fallback = None
    for name, path in cands:
        ver = _exe_version(path)
        if ver is None:
            continue
        ok = ver >= _MIN_RUNTIME[name]
        info = {"name": name, "path": path, "version": ".".join(map(str, ver)),
                "supported": ok, "args": ["--js-runtimes", f"{name}:{path}"] if ok else []}
        if ok:
            return info
        fallback = fallback or info
    return fallback or {"name": "", "path": "", "version": "", "supported": False, "args": []}


def runtime_problem(rt=None):
    """Human sentence for why downloads can't run, or "" if the runtime is fine."""
    rt = rt or js_runtime()
    if rt["supported"]:
        return ""
    if rt["name"]:
        need = ".".join(map(str, _MIN_RUNTIME[rt["name"]]))
        return (f"{rt['name'].capitalize()} {rt['version']} is too old for yt-dlp (needs {rt['name']} {need}+). "
                "Install Deno 2.3+ (run ./install.sh, or https://deno.com) and restart HomeFlix.")
    return ("No JavaScript runtime found. yt-dlp needs Deno 2.3+ (or Node 22+) to solve YouTube's "
            "challenge. Run ./install.sh, or install Deno from https://deno.com, and restart HomeFlix.")


def _env():
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def build_cmd(ids, out_dir, archive, cookie_file):
    """Port of Lasso's build_cmd() for a hand-picked list of ids. Returns
    (cmd, before_file, after_file, batch_file). Downloads always run with the
    pasted cookies (start_job refuses without them), so this always uses the
    web_embedded client.

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

    cmd = [sys.executable, "-m", "yt_dlp", *js_runtime()["args"],
           "--cookies", cookie_file,
           # With cookies attached YouTube forces the default clients into SABR
           # streaming (muxed HLS only, ~1080p max). web_embedded keeps the full
           # DASH ladder; `default` is the fallback for videos with embedding
           # disabled. See Lasso/CLAUDE.md and yt-dlp issue #12482.
           "--extractor-args", "youtube:player_client=web_embedded,default",
           # Fetches yt-dlp's n-challenge solver script; the JS runtime alone
           # isn't enough.
           "--remote-components", "ejs:github"]
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
    if "no longer valid" in t:
        return COOKIE_BAD
    if "not a bot" in t or "sign in" in t or ("cookies" in t and "invalid" in t):
        return "YouTube wants a sign-in. Paste fresh cookies above."
    if "private" in t or "members-only" in t or "join this channel" in t:
        return "Private or members-only. Cookies from an account that can see it are needed."
    if ("challenge" in t or "page needs to be reloaded" in t
            or "requested format is not available" in t):
        return (runtime_problem()
                or "yt-dlp couldn't solve YouTube's challenge. Try Update yt-dlp, then retry.")
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
                    "index": len(out) + 1,
                    # author, for refresh.py (flat listings carry it, no per-video request needed)
                    "channel": e.get("channel") or e.get("uploader") or "",
                    "channel_id": e.get("channel_id") or ""})


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
    if not os.path.isfile(cookie_path()):
        return None, "YouTube cookies are required for downloads. Paste them in the box at the top first."
    if cookie_status().get("expired"):
        return None, COOKIE_BAD
    problem = runtime_problem()
    if problem:   # would fail every video at the challenge step -- say so up front
        return None, problem
    try:
        free, _total = free_space(resolve_target(source.target_subdir))
    except ValueError as e:
        return None, str(e)
    if free is not None and free < MIN_FREE_BYTES:
        return None, f"Only {free / 1024 ** 2:.0f} MB free on the target disk."
    if active_job():
        return None, "A download is already running."
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

    if not os.path.isfile(cookie_path()):    # removed between click and start
        DownloadJob.objects.filter(pk=job_id).update(
            status=DownloadJob.FAILED, finished=timezone.now(),
            summary="YouTube cookies are required. Paste them first.")
        return
    bad = cookies_ok_for_job()
    if bad:     # dead cookies would quietly download as a logged-out visitor
        DownloadJob.objects.filter(pk=job_id).update(
            status=DownloadJob.FAILED, finished=timezone.now(),
            summary=(bad + " Nothing was downloaded.")[:250])
        return
    cmd, before, after, batch = build_cmd(ids, out_dir, archive, cookie_path())

    log, last_flush = [], 0.0
    cur_title, cur_pct = "", 0
    cookie_dead = False

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
            if "no longer valid" in line.lower() and not cookie_dead:
                # yt-dlp would carry on without the login: stop it instead.
                cookie_dead = True
                _write_check(False, COOKIE_BAD)
                _kill_tree(proc.pid)
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
    if cookie_dead:
        status = DownloadJob.FAILED
        summary = f"{COOKIE_BAD} ({ok} downloaded before it stopped.)"
    elif fail == 0:
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
