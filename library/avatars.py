"""Channel avatars ("channel icons") pulled from YouTube.

`Video.channel` is only a name, so the pull needs each channel's YouTube id. The
saved download playlists' cached listings carry `channel` + `channel_id` for
every entry (see downloader._flatten), so the name -> id map comes from them
without a request per video; a playlist whose cached listing predates that field
is fetched again first. Each channel's page is then asked for its avatar through
yt-dlp (no cookies needed), the image is downloaded and stored as a 240 px
square JPEG under THUMBNAIL_DIR/channels/<ChannelMeta.pk>.jpg. The UI draws it
inside a circle, like YouTube does.

Same shape as refresh.py: a background thread, status in a JSON file so any
gunicorn worker can report it.
"""
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import connection
from django.utils import timezone

from . import downloader, refresh
from .models import ChannelMeta, DownloadSource, Video

logger = logging.getLogger(__name__)

STALE_SECONDS = 180
WORKERS = 4
AVATAR_PX = 240


def avatar_dir():
    return os.path.join(settings.THUMBNAIL_DIR, "channels")


def avatar_file(pk):
    return os.path.join(avatar_dir(), f"{pk}.jpg")


def avatar_urls(names):
    """{channel name: url of its avatar} for the channels that have one."""
    out = {}
    for m in ChannelMeta.objects.filter(name__in=list(names), avatar_updated__isnull=False):
        if os.path.isfile(avatar_file(m.pk)):
            out[m.name] = f"/channel-avatar/{m.pk}/?v={int(m.avatar_updated.timestamp())}"
    return out


# ---- finding the id and the picture ------------------------------------------------------

def id_map(entry_lists):
    """{channel name: channel id} from playlist listings (first id seen wins)."""
    ids = {}
    for entries in entry_lists:
        for e in entries:
            name, cid = e.get("channel"), e.get("channel_id")
            if name and cid and name not in ids:
                ids[name] = cid
    return ids


def pick_avatar(thumbnails):
    """The best avatar URL in a yt-dlp channel `thumbnails` list: a square one
    (the banner entries are wide), preferring the biggest, else the one yt-dlp
    labels avatar_uncropped."""
    square = [t for t in thumbnails or [] if t.get("url") and t.get("width") and t.get("width") == t.get("height")]
    if square:
        return max(square, key=lambda t: t["width"])["url"]
    for t in thumbnails or []:
        if t.get("id") == "avatar_uncropped" and t.get("url"):
            return t["url"]
    return ""


def channel_avatar_url(channel_id, timeout=60):
    """Ask yt-dlp for the channel page's thumbnails; the avatar URL or ""."""
    if not channel_id.startswith("UC"):
        return ""
    cmd = [sys.executable, "-m", "yt_dlp", "--flat-playlist", "--playlist-items", "0", "--dump-single-json",
           "--no-warnings", "--ignore-errors", f"https://www.youtube.com/channel/{channel_id}"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=downloader._env(),
                           creationflags=downloader._NO_WINDOW, **downloader._TEXT)
        data = json.loads(r.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        return ""
    return pick_avatar(data.get("thumbnails"))


def save_avatar(url, dest):
    """Download `url`, scale it to a square JPEG at `dest`. True on success."""
    data = refresh._http_get(url)
    if not data or len(data) < 500:
        return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, src = tempfile.mkstemp(suffix=".img", dir=os.path.dirname(dest))
    out = src + ".out.jpg"
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        r = subprocess.run(["ffmpeg", "-y", "-i", src, "-frames:v", "1", "-q:v", "3",
                            "-vf", f"scale={AVATAR_PX}:{AVATAR_PX}:force_original_aspect_ratio=increase,crop={AVATAR_PX}:{AVATAR_PX}",
                            out], capture_output=True, timeout=60, creationflags=downloader._NO_WINDOW)
        if r.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0:
            return False
        os.replace(out, dest)
        return True
    except (subprocess.SubprocessError, OSError):
        return False
    finally:
        for p in (src, out):
            try:
                os.remove(p)
            except OSError:
                pass


def pull_one(meta):
    """Fetch and store one channel's avatar. True if it now has one."""
    url = channel_avatar_url(meta.channel_id)
    if not url or not save_avatar(url, avatar_file(meta.pk)):
        return False
    ChannelMeta.objects.filter(pk=meta.pk).update(avatar_updated=timezone.now())
    return True


# ---- job (status in a file so any gunicorn worker can report it) ---------------------------

def _status_file():
    return os.path.join(settings.YTDL_DIR, "avatars.json")


def status():
    try:
        with open(_status_file(), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {"state": "idle"}
    if st.get("state") == "running" and time.time() - st.get("at", 0) > STALE_SECONDS:
        return {**st, "state": "error", "msg": "Interrupted (server restarted?)"}
    return st


def _write(**st):
    st["at"] = time.time()
    os.makedirs(settings.YTDL_DIR, exist_ok=True)
    tmp = _status_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, _status_file())


def start_pull(force=False):
    """Run the pull in a background thread. False if one is already running."""
    if status().get("state") == "running":
        return False
    _write(state="running", phase="Starting…", done=0, total=0, saved=0, failed=0)
    threading.Thread(target=_run, args=(force, True), daemon=True).start()
    return True


def _run(force, close_db=False):
    try:
        _run_inner(force)
    except Exception as e:
        logger.exception("channel avatar pull crashed")
        _write(state="error", msg=f"Crashed: {e}"[:250])
    finally:
        if close_db:
            connection.close()


def _listing(src):
    try:
        return json.loads(src.entries_json or "[]")
    except ValueError:
        return []


def _run_inner(force):
    started = time.time()
    names = sorted(set(Video.objects.filter(missing=False).exclude(channel="").values_list("channel", flat=True)))
    if not names:
        _write(state="error", msg="No video has a channel yet. Run \"Update library data\" first (author / channel).")
        return
    sources = list(DownloadSource.objects.all())
    lists, notes = [], []
    for i, src in enumerate(sources, 1):
        entries = _listing(src)
        if entries and not any(e.get("channel_id") for e in entries):    # cached before channel ids were kept
            _write(state="running", phase=f"Reading playlist {i} of {len(sources)}: {src.name or src.url}",
                   done=0, total=0, saved=0, failed=0)
            name, fresh, err = downloader.fetch_playlist(src.url, timeout=300)
            if err:
                notes.append(f"{src.name or src.url}: {err}")
            else:
                src.name = name or src.name
                src.entries_json = json.dumps(fresh)
                src.last_fetched = timezone.now()
                src.save(update_fields=["name", "entries_json", "last_fetched"])
                entries = fresh
        lists.append(entries)
    ids = id_map(lists)

    metas, no_id = [], 0
    for name in names:
        meta, _ = ChannelMeta.objects.get_or_create(name=name)
        if ids.get(name) and meta.channel_id != ids[name]:
            meta.channel_id = ids[name]
            meta.save(update_fields=["channel_id"])
        if not meta.channel_id:
            no_id += 1
            continue
        if force or not os.path.isfile(avatar_file(meta.pk)):
            metas.append(meta)
    total = len(metas)
    counts = {"saved": 0, "failed": 0}
    lock = threading.Lock()
    done = [0]

    def work(meta, threaded=True):
        try:
            ok = pull_one(meta)
        except Exception:
            logger.exception("avatar failed for %s", meta.name)
            ok = False
        finally:
            if threaded:
                connection.close()          # each worker thread has its own DB connection
        with lock:
            done[0] += 1
            counts["saved" if ok else "failed"] += 1
            _write(state="running", phase=f"Channel icons ({done[0]} of {total})", done=done[0], total=total, **counts)
        return ok

    if metas:
        _write(state="running", phase=f"Channel icons (0 of {total})", done=0, total=total, **counts)
        if WORKERS > 1:
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                list(ex.map(work, metas))
        else:
            for meta in metas:
                work(meta, threaded=False)
    have = sum(1 for m in ChannelMeta.objects.filter(name__in=names) if os.path.isfile(avatar_file(m.pk)))
    msg = f"{counts['saved']} icon{'s' if counts['saved'] != 1 else ''} saved · {have} of {len(names)} channels have one"
    if counts["failed"]:
        msg += f" · {counts['failed']} couldn't be fetched"
    if no_id:
        msg += f" · {no_id} channel{'s' if no_id != 1 else ''} aren't in any saved playlist (no YouTube id)"
    if notes:
        msg += " · " + "; ".join(notes)
    _write(state="done", phase="Done", msg=msg, done=total, total=total, started=started, **counts)
