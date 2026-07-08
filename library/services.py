"""Media handling: probing, thumbnails, scanning, sidecar metadata."""
import json
import logging
import os
import re
import subprocess
from datetime import datetime, date

logger = logging.getLogger(__name__)

from django.conf import settings
from django.utils import timezone

from .models import Video


def _run(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return out.returncode, out.stdout, out.stderr
    except Exception as e:  # ffmpeg missing, timeout, etc.
        return 1, "", str(e)


_FFMPEG_BANNER_RE = re.compile(
    r"^(ffmpeg version|built with|configuration:|lib\w+\s+\d)"
)


def _ffmpeg_error_tail(stderr, n=300):
    """ffmpeg always prints its version/build banner as the *first* lines of
    stderr, success or failure — the actual fatal error is always further
    down. Strip the banner lines and return the last `n` chars of what's left
    so callers show the real reason instead of the banner."""
    lines = [ln for ln in (stderr or "").splitlines() if not _FFMPEG_BANNER_RE.match(ln.strip())]
    text = "\n".join(lines).strip()
    return text[-n:] if text else (stderr or "").strip()[-n:]


def probe(path):
    """Return dict of technical metadata via ffprobe, or {} on failure."""
    code, out, _ = _run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ])
    if code != 0 or not out:
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}

    info = {"duration_seconds": None, "width": None, "height": None,
            "video_codec": "", "audio_codec": "", "size_bytes": None}

    fmt = data.get("format", {})
    if fmt.get("duration"):
        try:
            info["duration_seconds"] = float(fmt["duration"])
        except ValueError:
            pass
    if fmt.get("size"):
        try:
            info["size_bytes"] = int(fmt["size"])
        except ValueError:
            pass

    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not info["video_codec"]:
            info["video_codec"] = stream.get("codec_name", "")
            info["width"] = stream.get("width")
            info["height"] = stream.get("height")
        elif stream.get("codec_type") == "audio" and not info["audio_codec"]:
            info["audio_codec"] = stream.get("codec_name", "")
    return info


def is_browser_playable(ext, video_codec):
    if ext.lower() in settings.NON_BROWSER_CONTAINERS:
        return False
    if (video_codec or "").lower() in settings.NON_BROWSER_VCODECS:
        return False
    return True


def generate_thumbnail(video, percent=0.0):
    """Grab a frame at `percent` of the duration and save a JPEG. Returns path or ''."""
    os.makedirs(settings.THUMBNAIL_DIR, exist_ok=True)
    out_path = os.path.join(settings.THUMBNAIL_DIR, f"video_{video.id}.jpg")
    duration = video.duration_seconds or 0
    seek = max(0.0, (percent / 100.0) * duration)

    code, _, _ = _run([
        "ffmpeg", "-y", "-ss", f"{seek:.3f}", "-i", video.file_path,
        "-frames:v", "1", "-vf", "scale=480:-1", "-q:v", "3", out_path,
    ])
    if code == 0 and os.path.exists(out_path):
        video.thumbnail_path = out_path
        video.thumbnail_percent = percent
        video.save(update_fields=["thumbnail_path", "thumbnail_percent"])
        return out_path
    return ""


def generate_collage_thumbnail(obj, videos, out_path):
    """Build a 2x2 collage from (up to) 4 given videos and save it as `obj`'s
    (a Playlist or SmartPlaylist) cover at `out_path`. Fewer than 4 videos
    still fills all four corners by cycling what's there.

    Returns (path, error) — path is '' on failure, with error one of
    "no_videos", "no_thumbnails", or "ffmpeg: <stderr>" so the caller can
    show the *real* reason instead of guessing.
    """
    import itertools

    if not videos:
        return "", "no_videos"

    thumbs = []
    for v in videos:
        if not v.thumbnail_path or not os.path.exists(v.thumbnail_path):
            generate_thumbnail(v, percent=0.0)
        if v.thumbnail_path and os.path.exists(v.thumbnail_path):
            thumbs.append(v.thumbnail_path)
    if not thumbs:
        return "", "no_thumbnails"
    corners = list(itertools.islice(itertools.cycle(thumbs), 4))

    os.makedirs(settings.THUMBNAIL_DIR, exist_ok=True)
    cmd = ["ffmpeg", "-y"]
    for c in corners:
        cmd += ["-i", c]
    # setsar=1 + format=yuv420p: hstack/vstack hard-fail ("parameters do not
    # match") if the combined inputs disagree on sample-aspect-ratio or pixel
    # format, which happens easily here since scale alone doesn't reset SAR
    # and these thumbnails often came from source videos of different
    # original aspect ratios/pixel formats.
    filt = (
        "[0:v]scale=240:135,setsar=1,format=yuv420p[a];"
        "[1:v]scale=240:135,setsar=1,format=yuv420p[b];"
        "[2:v]scale=240:135,setsar=1,format=yuv420p[c];"
        "[3:v]scale=240:135,setsar=1,format=yuv420p[d];"
        "[a][b]hstack=inputs=2[top];[c][d]hstack=inputs=2[bottom];"
        "[top][bottom]vstack=inputs=2[out]"
    )
    cmd += ["-filter_complex", filt, "-map", "[out]", "-frames:v", "1", "-q:v", "3", out_path]

    code, _, err = _run(cmd)
    if code == 0 and os.path.exists(out_path):
        obj.thumbnail_path = out_path
        obj.save(update_fields=["thumbnail_path"])
        return out_path, None
    return "", f"ffmpeg: {_ffmpeg_error_tail(err)}" if err else f"ffmpeg exit {code}"


def read_sidecar(path):
    """If a yt-dlp .info.json sits next to the video, pull title/desc/channel/url/date."""
    base = os.path.splitext(path)[0]
    for candidate in (base + ".info.json", base + ".json"):
        if os.path.exists(candidate):
            try:
                with open(candidate, encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                return {}
            meta = {
                "title": d.get("title", ""),
                "description": d.get("description", "") or "",
                "channel": d.get("channel") or d.get("uploader") or "",
                "source_url": d.get("webpage_url") or d.get("original_url") or "",
            }
            ud = d.get("upload_date")  # YYYYMMDD
            if ud and len(ud) == 8:
                try:
                    meta["upload_date"] = date(int(ud[:4]), int(ud[4:6]), int(ud[6:8]))
                except ValueError:
                    pass
            return meta
    return {}


def scan_library(root=None, make_thumbs=True):
    """Walk the library, add new videos, update existing, flag deletions.
    Returns a summary dict. Non-video files are completely ignored.
    """
    root = root or settings.LIBRARY_ROOT
    summary = {"added": 0, "updated": 0, "missing": 0, "root": root, "error": ""}
    if not os.path.isdir(root):
        summary["error"] = f"Library folder not found: {root}"
        return summary

    seen_paths = set()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext not in settings.VIDEO_EXTENSIONS:
                continue  # leave txt/png/jpeg and other files untouched
            full = os.path.join(dirpath, name)
            seen_paths.add(full)
            rel = os.path.relpath(full, root)
            mtime = timezone.make_aware(
                datetime.fromtimestamp(os.path.getmtime(full))
            )

            video = Video.objects.filter(file_path=full).first()
            if video:
                # Re-probe only if file changed size/mtime
                changed = (video.file_mtime != mtime)
                video.rel_path = rel
                video.filename = name
                video.ext = ext.lstrip(".")
                video.file_mtime = mtime
                video.missing = False
                if changed:
                    info = probe(full)
                    for k, v in info.items():
                        setattr(video, k, v)
                    video.browser_playable = is_browser_playable(ext, video.video_codec)
                video.save()
                summary["updated"] += 1
                continue

            # New video
            info = probe(full)
            sidecar = read_sidecar(full)
            video = Video.objects.create(
                file_path=full,
                rel_path=rel,
                filename=name,
                ext=ext.lstrip("."),
                title=sidecar.get("title") or os.path.splitext(name)[0],
                description=sidecar.get("description", ""),
                channel=sidecar.get("channel", ""),
                source_url=sidecar.get("source_url", ""),
                upload_date=sidecar.get("upload_date"),
                file_mtime=mtime,
                browser_playable=is_browser_playable(ext, info.get("video_codec", "")),
                **info,
            )
            if make_thumbs:
                generate_thumbnail(video, percent=0.0)
            summary["added"] += 1

    # Flag any DB rows whose files vanished
    gone = Video.objects.exclude(file_path__in=seen_paths).filter(missing=False)
    summary["missing"] = gone.count()
    gone.update(missing=True)
    return summary


# ---- Conversion (MKV / HEVC -> browser-friendly MP4) -----------------------
import threading
import shutil

# Sidecar extensions that belong to a video and should move/convert alongside it.
SIDECAR_EXTS = (".srt", ".vtt", ".ass", ".info.json", ".json",
                ".nfo", ".jpg", ".jpeg", ".png", ".webp")


# Track the running ffmpeg Popen per video so cancel_conversion() can stop it,
# and which video ids were just explicitly cancelled so _ffmpeg_convert's own
# post-process status update (which runs in the background thread, racing
# with cancel_conversion()'s reset) doesn't clobber it back to "failed".
_CONVERT_PROCS = {}
_CANCELLED = set()


def _ffmpeg_convert(video_id):
    from .models import Video
    video = Video.objects.filter(id=video_id).first()
    if not video:
        return
    video.convert_status = Video.CONVERT_RUNNING
    video.convert_progress = 0
    video.save(update_fields=["convert_status", "convert_progress"])

    os.makedirs(settings.CONVERTED_DIR, exist_ok=True)
    out_path = os.path.join(settings.CONVERTED_DIR, f"video_{video.id}.mp4")

    # Copy streams the browser already supports; transcode only what it can't.
    vcodec = (video.video_codec or "").lower()
    acodec = (video.audio_codec or "").lower()
    v_args = ["-c:v", "copy"] if vcodec in settings.REMUX_SAFE_VCODECS else \
             ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    a_args = ["-c:a", "copy"] if acodec in settings.REMUX_SAFE_ACODECS else ["-c:a", "aac", "-b:a", "192k"]

    cmd = ["ffmpeg", "-y", "-i", video.file_path, *v_args, *a_args,
           "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out_path]

    duration = video.duration_seconds or 0
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True)
        _CONVERT_PROCS[video.id] = proc
        try:
            for line in proc.stdout:
                if line.startswith("out_time_ms=") and duration:
                    try:
                        secs = int(line.strip().split("=")[1]) / 1_000_000
                        pct = max(0, min(99, int(secs / duration * 100)))
                        Video.objects.filter(id=video.id).update(convert_progress=pct)
                    except (ValueError, ZeroDivisionError):
                        pass
            proc.wait()
            ok = proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0
        finally:
            _CONVERT_PROCS.pop(video.id, None)
    except Exception:
        ok = False

    if video.id in _CANCELLED:
        # cancel_conversion() already reset status + cleaned up the partial
        # file — don't overwrite that with a stale "failed" from the process
        # we just killed.
        _CANCELLED.discard(video.id)
        return

    video.refresh_from_db()
    if ok:
        video.convert_status = Video.CONVERT_DONE
        video.converted_path = out_path
        video.convert_progress = 100
    else:
        video.convert_status = Video.CONVERT_FAILED
    video.save(update_fields=["convert_status", "converted_path", "convert_progress"])


def cancel_conversion(video):
    """Stop a running/queued conversion and reset state so the user can retry
    or the file just goes back to needing (or not needing) conversion."""
    proc = _CONVERT_PROCS.get(video.id)
    if proc:
        _CANCELLED.add(video.id)
        try:
            proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    out_path = os.path.join(settings.CONVERTED_DIR, f"video_{video.id}.mp4")
    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except OSError:
            pass  # e.g. Windows still holding the handle until ffmpeg exits
    Video.objects.filter(id=video.id).update(
        convert_status=Video.CONVERT_NONE, convert_progress=0)


def start_conversion(video):
    """Kick off conversion in a background thread; returns immediately."""
    from .models import Video
    if video.convert_status in (Video.CONVERT_QUEUED, Video.CONVERT_RUNNING):
        return
    video.convert_status = Video.CONVERT_QUEUED
    video.convert_progress = 0
    video.save(update_fields=["convert_status", "convert_progress"])
    t = threading.Thread(target=_ffmpeg_convert, args=(video.id,), daemon=True)
    t.start()


# ---- Organize files into YYYY-MM/DD folders by modified date ---------------
def _sidecars_for(path):
    base = os.path.splitext(path)[0]
    found = []
    for ext in SIDECAR_EXTS:
        cand = base + ext
        if os.path.exists(cand):
            found.append(cand)
    return found


def organize_by_mtime(execute=False):
    """Plan (and optionally perform) moving each video into LIBRARY_ROOT/YYYY-MM/DD/.
    Moves the video plus its sidecars; leaves unrelated files untouched.
    Returns {"moves": [(rel_src, rel_dst), ...], "count": n, "executed": bool}.
    """
    from .models import Video
    root = settings.LIBRARY_ROOT
    moves = []
    for v in Video.objects.filter(missing=False):
        if not v.file_mtime or not os.path.exists(v.file_path):
            continue
        folder = v.file_mtime.strftime("%Y-%m/%d")  # e.g. 2026-06/18
        dest_dir = os.path.join(root, folder)
        dest = os.path.join(dest_dir, v.filename)
        if os.path.abspath(dest) == os.path.abspath(v.file_path):
            continue  # already in place
        moves.append({
            "video_id": v.id,
            "src": os.path.relpath(v.file_path, root),
            "dst": os.path.relpath(dest, root),
            "sidecars": [os.path.basename(s) for s in _sidecars_for(v.file_path)],
        })
        if execute:
            os.makedirs(dest_dir, exist_ok=True)
            # move sidecars first (same base name)
            for sc in _sidecars_for(v.file_path):
                sc_dst = os.path.join(dest_dir, os.path.basename(sc))
                if os.path.abspath(sc) != os.path.abspath(sc_dst):
                    shutil.move(sc, sc_dst)
            shutil.move(v.file_path, dest)
            v.file_path = dest
            v.rel_path = os.path.relpath(dest, root)
            v.save(update_fields=["file_path", "rel_path"])
    return {"moves": moves, "count": len(moves), "executed": execute}


# ---- Library maintenance ----------------------------------------------------
def purge_missing():
    from .models import Video
    qs = Video.objects.filter(missing=True)
    n = qs.count()
    qs.delete()
    return n


def reset_library():
    """Wipe all video records (keeps files on disk) so a fresh scan rebuilds."""
    from .models import Video
    n = Video.objects.count()
    Video.objects.all().delete()
    return n
