"""The library's folders.

Any number of folders make up the library: every one is scanned and watched,
and exactly one is the *main* folder, where downloads are saved. They live in
the `LibraryFolder` table and are edited on the "Library folders" page.

With no rows (a fresh install, or an upgrade from the single-folder days) the
library is just `settings.LIBRARY_ROOT` (`HOMEFLIX_LIBRARY`), which is also the
main folder -- so nothing changes until the owner saves a list. Tests that
override `LIBRARY_ROOT` rely on exactly that.

`Video.file_path` is absolute, so adding, removing or re-pointing a folder never
touches existing rows; a folder that is taken away just makes its videos
"missing" on the next scan (history, notes and playlists stay on the row).
"""
import os
import shutil

from django.conf import settings
from django.db import transaction

from .models import LibraryFolder, Video

MAX_FOLDERS = 30


def _norm(path):
    return os.path.normpath(path)


def config():
    """[{"path", "remote", "main"}] in display order. Never empty."""
    rows = list(LibraryFolder.objects.all())
    if not rows:
        return [{"path": _norm(settings.LIBRARY_ROOT), "remote": settings.REMOTE_ROOT or "", "main": True}]
    main = next((r for r in rows if r.is_main), rows[0])
    return [{"path": _norm(r.path), "remote": r.remote, "main": r is main} for r in rows]


def library_roots():
    return [f["path"] for f in config()]


def main_root():
    return next(f["path"] for f in config() if f["main"])


def is_custom():
    """True once the owner has saved a folder list (vs the HOMEFLIX_LIBRARY default)."""
    return LibraryFolder.objects.exists()


def prefix(root):
    """`root` + separator, so /a/b never claims /a/bc/x."""
    return root.rstrip(os.sep) + os.sep


def root_of(path, roots=None):
    """The configured folder that contains `path` (the deepest, should two
    ever overlap), or None."""
    p = _norm(path)
    best = None
    for r in (roots if roots is not None else library_roots()):
        if (p == r or p.startswith(prefix(r))) and (best is None or len(r) > len(best)):
            best = r
    return best


def rel_to_root(path, roots=None):
    """`path` relative to the folder it lives in (falls back to the main folder)."""
    roots = roots if roots is not None else library_roots()
    root = root_of(path, roots) or roots[0]
    return os.path.relpath(path, root)


def remote_path(video):
    """What the player shows so you can jump to the file from another machine:
    the folder's network path + the relative path, else the real local path."""
    cfg = config()
    root = root_of(video.file_path, [f["path"] for f in cfg])
    remote = next((f["remote"] for f in cfg if f["path"] == root), "") if root else ""
    if not remote:
        return video.file_path
    rel = os.path.relpath(video.file_path, root)
    return remote.rstrip("\\/") + "\\" + rel.replace("/", "\\")


def _real(p):
    return os.path.realpath(p)


def save_config(folders, main):
    """Replace the folder list. `folders` = [{"path", "remote"}], `main` = one of
    the paths. Raises ValueError with a message meant for the user.

    A folder that is already saved is kept even while it is offline (an
    unmounted drive must not stop you editing the others); a *new* one has to
    exist right now, which catches typos."""
    if not isinstance(folders, list) or not folders:
        raise ValueError("Add at least one folder")
    if len(folders) > MAX_FOLDERS:
        raise ValueError(f"At most {MAX_FOLDERS} folders")
    known = {_norm(r.path) for r in LibraryFolder.objects.all()}
    known.add(_norm(settings.LIBRARY_ROOT))      # the env default counts as already known
    clean, seen = [], []
    for f in folders:
        raw = str((f or {}).get("path") or "").strip()
        remote = str((f or {}).get("remote") or "").strip()[:1024]
        if not raw:
            raise ValueError("A folder path is empty")
        if not os.path.isabs(raw):
            raise ValueError(f"Use the full path, starting with / : {raw}")
        path = _norm(raw)
        if path == os.path.dirname(path):
            raise ValueError("Not the whole disk: pick a folder")
        if os.path.isdir(path):
            if not os.access(path, os.R_OK | os.X_OK):
                raise ValueError(f"Can't read {path}")
        elif path not in known:
            raise ValueError(f"No such folder: {path}")
        real = _real(path)
        for other, other_real in seen:
            if real == other_real:
                raise ValueError(f"{path} is listed twice")
            if real.startswith(prefix(other_real)) or other_real.startswith(prefix(real)):
                raise ValueError(f"{path} and {other} overlap: one is inside the other")
        seen.append((path, real))
        clean.append({"path": path, "remote": remote})
    main_path = _norm(str(main or "").strip()) if main else ""
    if main_path not in {c["path"] for c in clean}:
        raise ValueError("Pick which folder downloads go to")
    if os.path.isdir(main_path) and not os.access(main_path, os.W_OK):
        raise ValueError(f"Downloads can't be saved to {main_path}: it is read-only for HomeFlix")
    with transaction.atomic():
        LibraryFolder.objects.exclude(path__in=[c["path"] for c in clean]).delete()
        for i, c in enumerate(clean):
            LibraryFolder.objects.update_or_create(
                path=c["path"],
                defaults={"remote": c["remote"], "position": i, "is_main": c["path"] == main_path})
    return status()


def _disk(path):
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


def status():
    """Everything the folders page shows: per folder, whether it is reachable,
    how many videos it holds and how much room is left."""
    out = []
    for f in config():
        path = f["path"]
        online = os.path.isdir(path)
        qs = Video.objects.filter(file_path__startswith=prefix(path))
        free, total = _disk(path) if online else (None, None)
        out.append({
            **f,
            "online": online,
            "writable": online and os.access(path, os.W_OK),
            "videos": qs.filter(missing=False).count(),
            "missing": qs.filter(missing=True).count(),
            "free": free, "total": total,
        })
    return {"folders": out, "custom": is_custom(), "env_default": _norm(settings.LIBRARY_ROOT)}
