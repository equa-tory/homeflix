"""Automatic backups of HomeFlix's own data (not the videos).

A backup is one `homeflix-YYYYmmdd-HHMMSS.tar.gz` holding a consistent snapshot
of the database (favorites, ratings, playlists, notes, history, accounts, saved
download sources, settings) plus `thumbnails/` (which holds uploaded playlist
covers that can't be regenerated) and `subtitles/` (uploaded subtitles). The
videos themselves, the regenerable `converted/`, `hls/`, `cache/` and
`thumbnails/frames/`, and secrets (`homeflix.env`, `ytdl/cookies.txt`) are
deliberately left out.

Settings live in `Setting` rows (keys `backup_*`) so there is no migration.
"""
import io
import json
import logging
import os
import re
import tarfile
import tempfile
import threading
import time
from datetime import datetime

from django.conf import settings
from django.db import connection

from .models import Setting

logger = logging.getLogger(__name__)

DEFAULTS = {"enabled": "1", "hours": "48", "path": "/mnt/ssd/backups/homeflix", "keep": "1"}
NAME_RE = re.compile(r"^homeflix-\d{8}-\d{6}\.tar\.gz$")   # the only files we ever create or delete
MAX_HOURS = 24 * 90
MAX_KEEP = 50
RUNNING_STALE = 3600        # a "running" flag older than this is a crashed run
LOCK_WAIT = 120             # give up if the database stays locked this long (seconds)
_SQLITE_BUSY, _SQLITE_LOCKED = 5, 6


# ---- shared: one-worker-wins scheduling -------------------------------------

def claim(name, min_interval):
    """True for exactly one caller per `min_interval` seconds, across threads
    *and* gunicorn workers (compare-and-swap on a Setting row holding the time
    of the last claim). Used so the 3 workers' timers don't all scan / back up
    at once."""
    key = f"claim_{name}"
    row, _ = Setting.objects.get_or_create(key=key, defaults={"value": "0"})
    try:
        last = float(row.value)
    except ValueError:
        last = 0.0
    now = time.time()
    if now - last < min_interval:
        return False
    return Setting.objects.filter(key=key, value=row.value).update(value=repr(now)) == 1


# ---- configuration ----------------------------------------------------------

def _int(raw, default, lo, hi):
    try:
        return max(lo, min(hi, int(float(raw))))
    except (TypeError, ValueError):
        return default


def get_config():
    g = lambda k: Setting.get(f"backup_{k}", DEFAULTS[k])
    return {
        "enabled": g("enabled") == "1",
        "hours": _int(g("hours"), 48, 1, MAX_HOURS),
        "path": g("path").strip(),
        "keep": _int(g("keep"), 1, 1, MAX_KEEP),
    }


def _protected_dirs():
    return [os.path.realpath(d) for d in (settings.THUMBNAIL_DIR, settings.SUBTITLE_DIR)]


def validate_path(path):
    """Normalised absolute backup folder, or ValueError. Refuses relative
    paths, the filesystem root, and folders inside what is being backed up
    (the archive would end up containing itself)."""
    p = (path or "").strip()
    if not p or not os.path.isabs(p):
        raise ValueError("Use a full path, like /mnt/ssd/backups/homeflix")
    p = os.path.normpath(p)
    if os.path.dirname(p) == p:
        raise ValueError("Pick a folder, not the filesystem root")
    real = os.path.realpath(p)
    for d in _protected_dirs():
        if real == d or real.startswith(d + os.sep):
            raise ValueError("The backup folder can't be inside the thumbnails/subtitles folders")
    return p


def ensure_writable(path):
    """Create the folder if needed and prove we can write to it."""
    try:
        os.makedirs(path, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".write-test-", dir=path)
        os.close(fd)
        os.remove(probe)
    except OSError as e:
        raise ValueError(f"Can't write to {path}: {e.strerror or e}")


def save_config(enabled, hours, path, keep):
    """Validate and store. Raises ValueError with a message for the UI."""
    try:
        h = int(float(hours))
    except (TypeError, ValueError):
        raise ValueError("Hours must be a number")
    if not 1 <= h <= MAX_HOURS:
        raise ValueError(f"Hours must be between 1 and {MAX_HOURS}")
    try:
        k = int(float(keep))
    except (TypeError, ValueError):
        raise ValueError("Max backups must be a number")
    if not 1 <= k <= MAX_KEEP:
        raise ValueError(f"Max backups must be between 1 and {MAX_KEEP}")
    p = validate_path(path)
    ensure_writable(p)
    Setting.set("backup_enabled", "1" if enabled else "0")
    Setting.set("backup_hours", h)
    Setting.set("backup_path", p)
    Setting.set("backup_keep", k)


# ---- status -----------------------------------------------------------------

def _ts(key):
    try:
        return float(Setting.get(key, "") or 0)
    except ValueError:
        return 0.0


def is_running():
    started = _ts("backup_running")
    return bool(started) and time.time() - started < RUNNING_STALE


def list_backups(path):
    out = []
    try:
        for e in os.scandir(path):
            if e.is_file() and NAME_RE.match(e.name):
                st = e.stat()
                out.append({"name": e.name, "size": st.st_size, "mtime": st.st_mtime})
    except OSError:
        pass
    return sorted(out, key=lambda f: f["name"], reverse=True)


def status():
    cfg = get_config()
    last_ok = _ts("backup_last_ok")
    next_due = None
    if cfg["enabled"]:
        next_due = (last_ok + cfg["hours"] * 3600) if last_ok else time.time()
    return {**cfg, "running": is_running(), "last_ok": last_ok or None,
            "last_msg": Setting.get("backup_last_msg", ""),
            "next_due": next_due, "files": list_backups(cfg["path"])}


# ---- doing the backup -------------------------------------------------------

def _tree_size(root, skip=()):
    total = 0
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if os.path.join(dp, d) not in skip]
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def _add_tree(tar, root, arcname, skip=()):
    """Add a folder file by file: one file vanishing mid-backup (a thumbnail
    regenerated, a subtitle cache rewritten) must not fail the whole run."""
    if not os.path.isdir(root):
        return 0
    n = 0
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if os.path.join(dp, d) not in skip]
        for f in files:
            full = os.path.join(dp, f)
            try:
                tar.add(full, arcname=os.path.join(arcname, os.path.relpath(full, root)), recursive=False)
                n += 1
            except OSError:
                continue
    return n


def prune(path, keep):
    """Delete the oldest of *our* backup files beyond `keep`. Names carry the
    timestamp, so name order is age order. Anything else in the folder is
    never touched."""
    for old in list_backups(path)[keep:]:
        try:
            os.remove(os.path.join(path, old["name"]))
        except OSError:
            logger.warning("couldn't remove old backup %s", old["name"])


def lock_watch(limit=None):
    """A progress callback for sqlite3's Connection.backup(). That call sleeps
    and retries *forever* while the source is busy/locked; this makes it fail
    after `limit` seconds of continuous blocking instead of hanging a thread
    (and leaving the "running" flag set until it goes stale)."""
    blocked = {"since": None}

    def progress(status, remaining, total):
        if status in (_SQLITE_BUSY, _SQLITE_LOCKED):
            now = time.time()
            blocked["since"] = blocked["since"] or now
            if now - blocked["since"] > (LOCK_WAIT if limit is None else limit):
                raise TimeoutError("the database stayed locked")
        else:
            blocked["since"] = None

    return progress


def run_backup():
    """Make one backup now. Returns {"ok": bool, "msg": str, "file": name}.
    Never raises: the result is also stored for the UI."""
    cfg = get_config()
    Setting.set("backup_running", repr(time.time()))
    part = snap = None
    try:
        path = validate_path(cfg["path"])
        ensure_writable(path)
        frames = os.path.join(settings.THUMBNAIL_DIR, "frames")
        est = _tree_size(settings.THUMBNAIL_DIR, (frames,)) + _tree_size(settings.SUBTITLE_DIR)
        try:
            est += os.path.getsize(settings.DATABASES["default"]["NAME"])
        except (OSError, TypeError):
            pass
        free = _free(path)
        if free is not None and free < est * 2 + 50 * 1024 * 1024:
            raise ValueError(f"Not enough free space in {path} ({free // 1024 ** 2} MB free, "
                             f"need about {est * 2 // 1024 ** 2} MB)")

        final = os.path.join(path, f"homeflix-{datetime.now():%Y%m%d-%H%M%S}.tar.gz")
        part = final + ".part"
        # Snapshot the live database with SQLite's online backup API: a plain
        # file copy can catch it mid-write. Lives next to the archive so a big
        # DB doesn't fill /tmp.
        fd, snap = tempfile.mkstemp(prefix=".db-snapshot-", suffix=".sqlite3", dir=path)
        os.close(fd)
        import sqlite3
        connection.ensure_connection()
        dst = sqlite3.connect(snap)
        try:
            connection.connection.backup(dst, progress=lock_watch())
        except (sqlite3.Error, TimeoutError) as e:
            raise ValueError(f"couldn't snapshot the database: {e}")
        finally:
            dst.close()

        counts = {}
        with tarfile.open(part, "w:gz") as tar:
            tar.add(snap, arcname="db.sqlite3")
            counts["thumbnails"] = _add_tree(tar, settings.THUMBNAIL_DIR, "thumbnails", (frames,))
            counts["subtitles"] = _add_tree(tar, settings.SUBTITLE_DIR, "subtitles")
            manifest = json.dumps({"app": "homeflix", "created": datetime.now().isoformat(timespec="seconds"),
                                   "files": counts}, indent=2).encode()
            info = tarfile.TarInfo("MANIFEST.json")
            info.size, info.mtime = len(manifest), time.time()
            tar.addfile(info, io.BytesIO(manifest))
        os.replace(part, final)
        part = None
        prune(path, cfg["keep"])
        size = os.path.getsize(final)
        msg = f"OK · {os.path.basename(final)} · {size / 1024 ** 2:.0f} MB"
        Setting.set("backup_last_ok", repr(time.time()))
        Setting.set("backup_last_msg", msg)
        return {"ok": True, "msg": msg, "file": os.path.basename(final)}
    except Exception as e:
        msg = f"Failed: {e}"
        logger.warning("backup failed: %s", e)
        Setting.set("backup_last_msg", msg)
        return {"ok": False, "msg": msg, "file": ""}
    finally:
        for tmp in (part, snap):
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        Setting.set("backup_running", "")


def _free(path):
    import shutil
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def maybe_run():
    """Scheduler tick (every worker, every 10 min): back up if enabled and due.
    claim() makes sure only one worker does it, and spaces retries after a
    failure to an hour instead of every tick."""
    cfg = get_config()
    if not cfg["enabled"] or is_running():
        return None
    if time.time() - _ts("backup_last_ok") < cfg["hours"] * 3600:
        return None
    if not claim("backup", 3600):
        return None
    return run_backup()


def start_now():
    """Manual "Back up now": run in a thread. False if one is already running."""
    if is_running():
        return False

    def go():
        try:
            run_backup()
        finally:
            connection.close()

    Setting.set("backup_running", repr(time.time()))
    threading.Thread(target=go, daemon=True).start()
    return True
