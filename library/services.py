"""Media handling: probing, thumbnails, scanning, sidecar metadata."""
import hashlib
import json
import logging
import os
import re
import subprocess
from datetime import datetime, date

logger = logging.getLogger(__name__)

from django.conf import settings
from django.utils import timezone

from .models import Video, VideoSubtitle


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
    # Composite via overlay onto a solid canvas rather than hstack/vstack —
    # hstack/vstack hard-crash on some ffmpeg builds (seen: ffmpeg 6.1.1
    # "malloc(): invalid size (unsorted)") when the four inputs disagree on
    # size/SAR/pixel format, which happens easily here since these thumbnails
    # come from source videos of different original aspect ratios/formats.
    # overlay is far more tolerant of that mismatch.
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=480x270"]
    for c in corners:
        cmd += ["-i", c]
    filt = (
        "[1:v]scale=240:135,setsar=1[a];[2:v]scale=240:135,setsar=1[b];"
        "[3:v]scale=240:135,setsar=1[c];[4:v]scale=240:135,setsar=1[d];"
        "[0:v][a]overlay=0:0[s1];[s1][b]overlay=240:0[s2];"
        "[s2][c]overlay=0:135[s3];[s3][d]overlay=240:135,format=yuv420p[out]"
    )
    cmd += ["-filter_complex", filt, "-map", "[out]", "-frames:v", "1", "-q:v", "3", out_path]

    code, _, err = _run(cmd)
    if code == 0 and os.path.exists(out_path):
        obj.thumbnail_path = out_path
        obj.save(update_fields=["thumbnail_path"])
        return out_path, None

    # Collage failed for some ffmpeg-specific reason — don't leave the user
    # with nothing when we already have a perfectly good thumbnail to use.
    try:
        import shutil
        shutil.copyfile(thumbs[0], out_path)
        obj.thumbnail_path = out_path
        obj.save(update_fields=["thumbnail_path"])
        return out_path, None
    except OSError:
        pass
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

    # Always re-encode both streams. Stream-copying video while re-encoding
    # audio was tried first (fast, byte-exact video) but left the copied
    # video's original (sometimes irregular/VFR) timestamps out of sync with
    # the freshly-encoded audio's clean ones — that mismatch is what caused
    # playback to stutter/freeze. A full re-encode gives both streams one
    # consistent, clean timeline. crf 20 is close to visually lossless.
    cmd = ["ffmpeg", "-y", "-i", video.file_path,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-ac", "2",
           "-sn",  # drop any subtitle stream -- mp4 can't hold ass/srt and ffmpeg's
                   # default stream selection would otherwise try to include it and
                   # fail the whole conversion. Subtitles are served separately (see
                   # services.list_subtitles / ensure_subtitle_vtt).
           "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out_path]

    duration = video.duration_seconds or 0
    stderr_lines = []
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _CONVERT_PROCS[video.id] = proc

        def _drain_stderr(pipe):
            try:
                for line in iter(pipe.readline, ""):
                    if line:
                        stderr_lines.append(line)
            except Exception:
                pass

        threading.Thread(target=_drain_stderr, args=(proc.stderr,), daemon=True).start()
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

    if not ok and video_id not in _CANCELLED:
        # Previously discarded entirely (stderr=DEVNULL) — a conversion
        # failure had zero visibility. Log the real ffmpeg error so it's at
        # least in the server log instead of a silent "failed" status.
        logger.warning("conversion failed for video %s: %s",
                        video_id, _ffmpeg_error_tail("".join(stderr_lines)))

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


# ---- Live HLS transcode (Jellyfin-style on-the-fly playback) ---------------
# For non-browser-playable files we transcode to HLS (short .ts segments + an
# .m3u8 playlist) live: playback starts after the first couple of segments while
# ffmpeg keeps producing the rest. hls.js (or native HLS on Apple) plays it.
#
# Sessions are tracked on the FILESYSTEM (a per-video dir + an ffmpeg.pid file),
# NOT in an in-memory dict — under gunicorn there are several worker processes,
# each with its own memory, all sharing this filesystem. An in-memory registry
# let two workers each think "no session yet", each rmtree the shared segment
# dir and spawn a competing ffmpeg, deleting each other's segments mid-write
# ("Failed to open seg_00000.ts"). A cross-process flock single-flight + a
# pid-file liveness check fixes that: whichever worker gets the lock starts (or
# confirms) the one transcode; everyone else reuses it.
import time
import signal

try:
    import fcntl
    _HAVE_FCNTL = True
except ImportError:              # Windows dev box (the server itself runs on Linux)
    _HAVE_FCNTL = False

_HLS_FALLBACK_LOCK = threading.Lock()
HLS_SEG_SECONDS = 4
HLS_TTL = 1800                   # reap dead session dirs idle longer than this


def _hls_dir(pk):
    return os.path.join(settings.HLS_DIR, str(pk))


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    return True


def _hls_read_pid(d):
    try:
        with open(os.path.join(d, "ffmpeg.pid")) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _hls_live(d):
    """A transcode is live if the ffmpeg it recorded is still running."""
    pid = _hls_read_pid(d)
    return bool(pid and _pid_alive(pid))


def _hls_ready(d):
    m = os.path.join(d, "index.m3u8")
    return os.path.exists(m) and os.path.getsize(m) > 0


def _hls_seg_count(d):
    try:
        return sum(1 for f in os.listdir(d)
                   if f.startswith("seg_") and f.endswith(".ts"))
    except OSError:
        return 0


def _hls_complete(d):
    """A finished transcode leaves a playlist with #EXT-X-ENDLIST and all its
    segments on disk — reuse it instead of re-transcoding on replay/seek-back."""
    try:
        with open(os.path.join(d, "index.m3u8")) as f:
            return "#EXT-X-ENDLIST" in f.read()
    except Exception:
        return False


class _hls_lock:
    """Cross-process single-flight (flock), with a thread-lock fallback where
    fcntl is unavailable. Serializes (re)starting/stopping a video's transcode
    so concurrent gunicorn workers can't stomp each other's segment dir."""
    def __init__(self, pk):
        self.path = _hls_dir(pk) + ".lock"
        self.f = None

    def __enter__(self):
        if _HAVE_FCNTL:
            self.f = open(self.path, "w")
            fcntl.flock(self.f, fcntl.LOCK_EX)
        else:
            _HLS_FALLBACK_LOCK.acquire()
        return self

    def __exit__(self, *exc):
        try:
            if _HAVE_FCNTL:
                fcntl.flock(self.f, fcntl.LOCK_UN)
                self.f.close()
            else:
                _HLS_FALLBACK_LOCK.release()
        except Exception:
            pass


def _drain_hls(pipe, vid):
    try:
        for line in iter(pipe.readline, b""):
            if line:
                logger.warning("hls ffmpeg (video %s): %s", vid,
                               line.decode(errors="replace").rstrip())
    except Exception:
        pass


def _hls_reap():
    """Remove dead session dirs whose transcode has finished/exited and which
    haven't been touched in a while. Cheap — only a handful of dirs ever exist."""
    import shutil
    root = settings.HLS_DIR
    now = time.time()
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        d = os.path.join(root, name)
        if not os.path.isdir(d) or _hls_live(d):
            continue
        try:
            idle = now - os.path.getmtime(d)
        except OSError:
            idle = HLS_TTL + 1
        if idle > HLS_TTL:
            shutil.rmtree(d, ignore_errors=True)


def start_hls(video):
    """Ensure a live HLS transcode is running for `video`; return its session
    dir (index.m3u8 + seg_*.ts). Cross-worker safe: concurrent callers reuse the
    one running transcode instead of restarting it."""
    import shutil
    pk = video.id
    d = _hls_dir(pk)
    os.makedirs(settings.HLS_DIR, exist_ok=True)
    with _hls_lock(pk):
        if _hls_live(d) or _hls_complete(d):
            return d
        # Nothing alive/complete -> clean up any stale pid/files and (re)start once.
        old = _hls_read_pid(d)
        if old:
            try:
                os.kill(old, signal.SIGKILL)
            except Exception:
                pass
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        # cwd=d + relative output names so the playlist lists segments as bare
        # "seg_00000.ts" (hls.js resolves those against the .m3u8 URL ->
        # /hls/<pk>/seg_00000.ts). Input path stays absolute. start_new_session
        # detaches ffmpeg so a recycled gunicorn worker doesn't take it down.
        cmd = [
            "ffmpeg", "-y", "-nostats", "-loglevel", "warning", "-i", video.file_path,
            # ultrafast (vs veryfast for the on-disk Convert): live playback wants
            # the whole file transcoded ASAP so seeking works sooner — the bigger
            # temp segments don't matter on a LAN. crf 23 keeps size sane.
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-force_key_frames", f"expr:gte(t,n_forced*{HLS_SEG_SECONDS})",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2",
            "-sn",  # drop any subtitle stream -- mpegts/hls can't carry ass/srt and
                    # ffmpeg's default stream selection would otherwise try to include
                    # it and fail the whole transcode. Subtitles are served separately
                    # (see services.list_subtitles / ensure_subtitle_vtt).
            "-f", "hls",
            "-hls_time", str(HLS_SEG_SECONDS),
            "-hls_playlist_type", "event",   # playlist grows; only lists ready segments
            "-hls_list_size", "0",
            "-hls_segment_type", "mpegts",
            "-hls_flags", "independent_segments",
            "-hls_segment_filename", "seg_%05d.ts",
            "index.m3u8",
        ]
        proc = subprocess.Popen(cmd, cwd=d, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, start_new_session=True)
        with open(os.path.join(d, "ffmpeg.pid"), "w") as f:
            f.write(str(proc.pid))
        threading.Thread(target=_drain_hls, args=(proc.stderr, pk), daemon=True).start()
        # Build a small head start (a few segments) before returning, so the
        # client opens with a buffer lead instead of playing right at the
        # transcode head (that underruns -> early stall / audio not started).
        # Also keeps the lock held through warmup so a second worker blocked on
        # it then sees _hls_live and reuses instead of restarting mid-warmup.
        for _ in range(100):     # up to ~10s
            if _hls_seg_count(d) >= 3 or _hls_complete(d) or proc.poll() is not None:
                break
            time.sleep(0.1)
        _hls_reap()
        return d


def touch_hls(pk):
    """Bump the session dir mtime so an actively-watched (but momentarily quiet)
    session isn't reaped. Segment writes already bump it; this covers gaps."""
    try:
        os.utime(_hls_dir(pk), None)
    except OSError:
        pass


def stop_hls(pk):
    import shutil
    with _hls_lock(pk):
        pid = _hls_read_pid(_hls_dir(pk))
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        shutil.rmtree(_hls_dir(pk), ignore_errors=True)


# ---- Subtitles (sidecar files + embedded mkv tracks -> WebVTT) -------------
# Only text-based subtitle formats can become WebVTT. Image subs (PGS/VOBSUB,
# bitmap .sub) are skipped entirely — no way to render those as a <track>.
SUB_SIDECAR_EXTS = (".vtt", ".srt", ".ass", ".ssa", ".sub")
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}

_LANG_LABELS = {
    "ru": "Russian", "rus": "Russian", "en": "English", "eng": "English",
    "ja": "Japanese", "jpn": "Japanese", "jp": "Japanese",
    "uk": "Ukrainian", "ukr": "Ukrainian", "de": "German", "ger": "German",
    "deu": "German", "fr": "French", "fre": "French", "fra": "French",
    "es": "Spanish", "spa": "Spanish", "it": "Italian", "ita": "Italian",
    "pt": "Portuguese", "por": "Portuguese", "zh": "Chinese", "chi": "Chinese",
    "zho": "Chinese", "ko": "Korean", "kor": "Korean", "und": "Unknown",
}


def _lang_label(code):
    code = (code or "").lower()
    return _LANG_LABELS.get(code, code.upper() if code else "")


def _sidecar_subtitles(path):
    """Sidecar subtitle files next to `path`: exact-basename matches
    (name.srt) and language-tagged ones (name.ru.ass, name.eng.srt)."""
    folder = os.path.dirname(path)
    base = os.path.splitext(os.path.basename(path))[0]
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        stem, ext = os.path.splitext(name)
        ext = ext.lower()
        if ext not in SUB_SIDECAR_EXTS:
            continue
        lang = ""
        if stem == base:
            pass
        elif stem.startswith(base + "."):
            lang = stem[len(base) + 1:]
        else:
            continue
        label = _lang_label(lang) or ext.lstrip(".").upper()
        out.append({
            "label": label, "lang": lang.lower(), "kind": "sidecar",
            "ref": os.path.join(folder, name),
        })
    out.sort(key=lambda t: t["label"])
    return out


def _embedded_subtitles(path):
    """Text-based subtitle streams inside the media file itself (mkv etc.),
    via ffprobe. Returns them in stream order with an ffmpeg -map-able index
    (0-based among *subtitle* streams)."""
    code, out, _ = _run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-select_streams", "s", path,
    ])
    if code != 0 or not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    result = []
    for i, stream in enumerate(data.get("streams", [])):
        codec = (stream.get("codec_name") or "").lower()
        if codec not in TEXT_SUB_CODECS:
            continue  # image subs (pgs/dvd_subtitle) can't become WebVTT
        tags = stream.get("tags", {}) or {}
        lang = tags.get("language", "")
        label = tags.get("title") or _lang_label(lang) or f"Track {i + 1}"
        result.append({
            "label": label, "lang": lang.lower(), "kind": "embedded",
            "ref": str(i),
        })
    return result


def _manual_subtitles(video):
    """User-uploaded subs (see store_uploaded_subtitle) -- already converted
    and app-owned, so they don't depend on anything still being on disk at
    the original location."""
    return [
        {"label": s.label, "lang": s.lang, "kind": "manual", "ref": s.vtt_path, "id": s.id}
        for s in video.uploaded_subtitles.all()
    ]


def list_subtitles(video):
    """All available subtitle tracks for a video: manual (uploaded) first,
    then sidecars, then embedded. Order is the index used in
    /subs/<pk>/<idx>.vtt for a given request -- see ensure_subtitle_vtt for
    why the on-disk *cache* filename doesn't depend on this index."""
    return (_manual_subtitles(video) + _sidecar_subtitles(video.file_path)
            + _embedded_subtitles(video.file_path))


def _sub_cache_key(track):
    """Stable identity for a track's cached .vtt filename -- NOT the list
    index, which shifts whenever a manual sub is added/removed and would
    otherwise serve a stale/wrong cached file after a reorder."""
    if track["kind"] == "manual":
        return f"manual{track['id']}"
    if track["kind"] == "embedded":
        return f"emb{track['ref']}"
    # sidecar: hash the absolute path -> stable across reorders, unique per file
    return "side" + hashlib.md5(track["ref"].encode("utf-8")).hexdigest()[:12]


def ensure_subtitle_vtt(video, idx):
    """Return the path to a cached WebVTT file for subtitle track `idx` of
    `video` (converting/extracting + caching on first request), or None if
    `idx` is out of range or conversion fails."""
    tracks = list_subtitles(video)
    if idx < 0 or idx >= len(tracks):
        return None
    track = tracks[idx]

    if track["kind"] == "manual":
        # Already a finished, app-owned .vtt -- nothing to convert/cache.
        return track["ref"] if os.path.exists(track["ref"]) else None

    os.makedirs(settings.SUBTITLE_DIR, exist_ok=True)
    out_path = os.path.join(settings.SUBTITLE_DIR,
                             f"video_{video.id}_{_sub_cache_key(track)}.vtt")

    src_mtime = 0.0
    if track["kind"] == "sidecar":
        try:
            src_mtime = os.path.getmtime(track["ref"])
        except OSError:
            return None
    if os.path.exists(out_path) and os.path.getmtime(out_path) >= src_mtime:
        return out_path

    if track["kind"] == "sidecar":
        src = track["ref"]
        if src.lower().endswith(".vtt"):
            try:
                shutil.copyfile(src, out_path)
                return out_path
            except OSError:
                return None
        code, _, err = _run(["ffmpeg", "-y", "-i", src, out_path])
    else:  # embedded
        code, _, err = _run([
            "ffmpeg", "-y", "-i", video.file_path,
            "-map", f"0:s:{track['ref']}", "-c:s", "webvtt", out_path,
        ])
    if code == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    logger.warning("subtitle conversion failed for video %s idx %s: %s",
                    video.id, idx, _ffmpeg_error_tail(err))
    return None


_SUB_UPLOAD_MAX_BYTES = 5 * 1024 * 1024  # subs are KB-sized; 5MB is generous


def store_uploaded_subtitle(video, upload, label, lang):
    """Save an uploaded subtitle file as an app-owned WebVTT, decoupled from
    the original file's location -- it survives the video being moved
    (organize) or the original .ass/.srt being deleted later.
    Returns the new VideoSubtitle row, or None (+ logs why) on failure."""
    import uuid

    name = upload.name or ""
    ext = os.path.splitext(name)[1].lower()
    if ext not in SUB_SIDECAR_EXTS:
        logger.warning("subtitle upload rejected for video %s: bad extension %r", video.id, ext)
        return None
    if upload.size > _SUB_UPLOAD_MAX_BYTES:
        logger.warning("subtitle upload rejected for video %s: too large (%s bytes)",
                        video.id, upload.size)
        return None

    os.makedirs(settings.SUBTITLE_DIR, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    out_path = os.path.join(settings.SUBTITLE_DIR, f"upload_{video.id}_{token}.vtt")
    tmp_path = os.path.join(settings.SUBTITLE_DIR, f"_tmp_{video.id}_{token}{ext}")
    try:
        with open(tmp_path, "wb") as f:
            for chunk in upload.chunks():
                f.write(chunk)
        if ext == ".vtt":
            shutil.copyfile(tmp_path, out_path)
            code = 0
        else:
            code, _, err = _run(["ffmpeg", "-y", "-i", tmp_path, out_path])
            if code != 0:
                logger.warning("subtitle upload conversion failed for video %s: %s",
                                video.id, _ffmpeg_error_tail(err))
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if code != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return None

    return VideoSubtitle.objects.create(
        video=video, label=label or (_lang_label(lang) or "Subtitle"),
        lang=(lang or "").lower(), vtt_path=out_path,
    )


def delete_uploaded_subtitle(sub):
    try:
        os.remove(sub.vtt_path)
    except OSError:
        pass
    sub.delete()


def cleanup_video_subtitles(video):
    """Remove every .vtt this video owns (uploaded + auto-converted cache)
    before the Video row itself is deleted -- mirrors the thumbnail/converted
    cleanup already done in views._delete_videos so nothing orphans on disk."""
    import glob
    for sub in video.uploaded_subtitles.all():
        try:
            os.remove(sub.vtt_path)
        except OSError:
            pass
    for path in glob.glob(os.path.join(settings.SUBTITLE_DIR, f"video_{video.id}_*.vtt")):
        try:
            os.remove(path)
        except OSError:
            pass


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
