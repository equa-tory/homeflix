"""Refresh already-downloaded videos' data (author, thumbnail, title) from YouTube.

Most library videos have no YouTube id stored (only ones downloaded with a
sidecar do), so they're matched *by name* against the saved download playlists
-- the same comparison the downloader uses -- and the playlist listing itself
carries everything needed (channel, real title, video id -> thumbnail URL), so
one fetch per playlist covers every video in it instead of a request per video.
Videos that aren't in any saved playlist are left alone.
"""
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

from django.conf import settings
from django.db import connection
from django.utils import timezone

from . import downloader
from .models import DownloadSource, Video

logger = logging.getLogger(__name__)

# Best first. maxresdefault/hq720 are true 16:9 (maxres is missing for some
# older videos); mqdefault is always there but small.
THUMB_CANDIDATES = ("maxresdefault", "hq720", "mqdefault")
STALE_SECONDS = 120          # a "running" status with no heartbeat this long is a dead run
HTTP_TIMEOUT = 20


# ---- matching -----------------------------------------------------------------

def merge_entries(entry_lists):
    """One entry per video id across all saved playlists, preferring an entry
    that carries the channel."""
    by_id = {}
    for entries in entry_lists:
        for e in entries:
            cur = by_id.get(e["id"])
            if cur is None or (not cur.get("channel") and e.get("channel")):
                by_id[e["id"]] = e
    return list(by_id.values())


def build_index(entries):
    """(by_id, by_name). A name shared by two *different* videos is ambiguous
    and dropped from by_name rather than guessed."""
    by_id, by_name, ambiguous = {}, {}, set()
    for e in entries:
        by_id[e["id"]] = e
        k = downloader.name_key(e.get("title"))
        if not k:
            continue
        if k in by_name and by_name[k]["id"] != e["id"]:
            ambiguous.add(k)
        by_name.setdefault(k, e)
    for k in ambiguous:
        by_name.pop(k, None)
    return by_id, by_name


def match_video(video, by_id, by_name):
    """The playlist entry for a library video: by stored YouTube id first,
    then by name (display title, then the file name)."""
    m = downloader.URL_ID_RE.search(video.source_url or "")
    if m and m.group(1) in by_id:
        return by_id[m.group(1)]
    for text in (video.title, os.path.splitext(video.filename or "")[0]):
        k = downloader.name_key(text)
        if k and k in by_name:
            return by_name[k]
    return None


# ---- applying -----------------------------------------------------------------

def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (HomeFlix)"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None


def fetch_thumbnail(video_id, dest):
    """Download YouTube's thumbnail for `video_id` and store it at `dest`,
    scaled to 480 px wide like services.generate_thumbnail does (so cards stay
    uniform and small). True on success; `dest` is untouched on failure."""
    data = None
    for name in THUMB_CANDIDATES:
        data = _http_get(f"https://i.ytimg.com/vi/{video_id}/{name}.jpg")
        if data and len(data) > 2000:
            break
        data = None
    if not data:
        return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, src = tempfile.mkstemp(suffix=".jpg", dir=os.path.dirname(dest))
    out = src + ".out.jpg"
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        code, _, _ = _run_ffmpeg(src, out)
        if code != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            return False
        os.replace(out, dest)
        return True
    finally:
        for p in (src, out):
            try:
                os.remove(p)
            except OSError:
                pass


def _run_ffmpeg(src, out):
    try:
        r = subprocess.run(["ffmpeg", "-y", "-i", src, "-frames:v", "1", "-vf", "scale=480:-1",
                            "-q:v", "3", out], capture_output=True, timeout=60,
                           creationflags=downloader._NO_WINDOW)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 1, b"", str(e).encode()


def apply_entry(video, entry, fields):
    """Write what `fields` asks for onto `video`. Returns a set of what
    changed ({"channel","title","thumb","url"}); raises nothing for a failed
    thumbnail download (that just isn't in the set)."""
    changed, update = set(), []
    yt_url = f"https://www.youtube.com/watch?v={entry['id']}"
    if video.source_url != yt_url:      # also makes later "already downloaded" checks exact
        video.source_url = yt_url
        update.append("source_url")
        changed.add("url")
    if fields.get("channel") and entry.get("channel") and video.channel != entry["channel"]:
        video.channel = entry["channel"][:256]
        update.append("channel")
        changed.add("channel")
    if fields.get("title") and entry.get("title") and video.title != entry["title"]:
        video.title = entry["title"][:512]
        update.append("title")
        changed.add("title")
    # Shorts keep their own frame: YouTube's thumbnail for a vertical video is
    # a landscape image with the video letterboxed in the middle.
    if fields.get("thumbs") and not video.is_vertical:
        dest = os.path.join(settings.THUMBNAIL_DIR, f"video_{video.id}.jpg")
        if fetch_thumbnail(entry["id"], dest):
            if video.thumbnail_path != dest:
                video.thumbnail_path = dest
                update.append("thumbnail_path")
            changed.add("thumb")
    if update:
        video.save(update_fields=update)
    return changed


# ---- job (status in a file so any gunicorn worker can report it) ---------------

def _status_file():
    return os.path.join(settings.YTDL_DIR, "refresh.json")


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
    tmp = _status_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, _status_file())


def start_refresh(fields):
    """Run the refresh in a background thread. False if one is already running."""
    if status().get("state") == "running":
        return False
    _write(state="running", phase="Starting…", fields=fields, started=time.time(),
           done=0, total=0, matched=0, changed=0, thumbs=0, failed=0)
    threading.Thread(target=_run, args=(fields, True), daemon=True).start()
    return True


def _run(fields, close_db=False):
    """Thread body; `close_db` only for the real thread (tests call it inline)."""
    try:
        _run_inner(fields)
    except Exception as e:
        logger.exception("library refresh crashed")
        _write(state="error", msg=f"Crashed: {e}"[:250], fields=fields)
    finally:
        if close_db:
            connection.close()


def _run_inner(fields):
    started = time.time()
    sources = list(DownloadSource.objects.all())
    if not sources:
        _write(state="error", msg="No saved playlists yet. Add the playlists your videos came from first.", fields=fields)
        return

    entry_lists, notes = [], []
    for i, src in enumerate(sources, 1):
        _write(state="running", phase=f"Fetching playlist {i} of {len(sources)}: {src.name or src.url}",
               fields=fields, started=started, done=0, total=0, matched=0, changed=0, thumbs=0, failed=0)
        name, entries, err = downloader.fetch_playlist(src.url, timeout=300)
        if err:
            try:
                cached = json.loads(src.entries_json or "[]")
            except ValueError:
                cached = []
            if cached:
                notes.append(f"{src.name or src.url}: couldn't refetch, used the saved list")
                entry_lists.append(cached)
            else:
                notes.append(f"{src.name or src.url}: {err}")
            continue
        src.name = name or src.name
        src.entries_json = json.dumps(entries)
        src.last_fetched = timezone.now()
        src.save(update_fields=["name", "entries_json", "last_fetched"])
        entry_lists.append(entries)

    by_id, by_name = build_index(merge_entries(entry_lists))
    videos = list(Video.objects.filter(missing=False))
    total = len(videos)
    counts = {"matched": 0, "changed": 0, "thumbs": 0, "failed": 0}
    last = 0.0
    for n, video in enumerate(videos, 1):
        entry = match_video(video, by_id, by_name)
        if entry:
            counts["matched"] += 1
            try:
                changed = apply_entry(video, entry, fields)
                if changed - {"url"}:
                    counts["changed"] += 1
                if "thumb" in changed:
                    counts["thumbs"] += 1
            except Exception:
                logger.exception("refresh failed for video %s", video.pk)
                counts["failed"] += 1
        if time.time() - last > 0.7:
            last = time.time()
            _write(state="running", phase=f"Updating videos ({n} of {total})", fields=fields,
                   started=started, done=n, total=total, **counts)
    msg = (f"Matched {counts['matched']} of {total} videos · {counts['changed']} updated"
           f" · {total - counts['matched']} aren't in any saved playlist")
    if counts["failed"]:
        msg += f" · {counts['failed']} failed"
    if notes:
        msg += " · " + "; ".join(notes)
    _write(state="done", phase="Done", msg=msg, fields=fields, started=started,
           done=total, total=total, **counts)
