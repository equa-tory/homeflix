# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

HomeFlix is a self-hosted, single-user video library built with Django + SQLite. It streams video from a local folder to any device on the LAN (PC, phone, LG TV). There is no authentication — it's designed for a trusted local network.

## Commands

```bash
# Set the video library path (required)
export HOMEFLIX_LIBRARY=/path/to/your/videos   # PowerShell: $env:HOMEFLIX_LIBRARY = "..."

# First-time setup
python manage.py migrate
python manage.py scan

# Development server (accessible on LAN)
python manage.py runserver 0.0.0.0:8002

# Run the library scanner manually
python manage.py scan

# Run tests
python manage.py test library

# Create admin user (for /admin bulk editing)
python manage.py createsuperuser
```

System dependency: `ffmpeg` and `ffprobe` must be on PATH (`apt install ffmpeg`). The only Python dependency is `Django>=5.0` (`requirements.txt`); production adds `gunicorn`. Everything else (fuzzy search, PNG icon generation, HLS session locking) is stdlib.

Note: `library/tests.py` is currently an empty stub — `python manage.py test library` runs zero tests. If you add tests, run a single one with `python manage.py test library.tests.<TestClass>.<test_method>`.

## Architecture

Django project package is `config/` (`config.settings`, `config.urls`, `config.wsgi:application`), with a single app `library/` holding all models/views/services.

**Key environment variables** (set in shell or systemd unit):
- `HOMEFLIX_LIBRARY` — root folder scanned recursively for video files
- `HOMEFLIX_ORIGINS` — comma-separated HTTPS origins for CSRF when behind nginx (e.g. `https://192.168.1.50:8443`)
- `HOMEFLIX_REMOTE_ROOT` — UNC path shown under the player so you can jump to the file from another machine (`REMOTE_ROOT` setting)

**Directories created at startup** (in `settings.py`, all outside `LIBRARY_ROOT` so the scanner never re-imports them): `thumbnails/`, `converted/`, `hls/`, `subtitles/`.

**Data flow for a new video:**
1. `services.scan_library()` walks `LIBRARY_ROOT`, calls `ffprobe` via `services.probe()`, reads optional yt-dlp `.info.json` sidecars via `services.read_sidecar()`, creates `Video` rows (re-probing only when mtime changed), and generates a thumbnail at 0% via `services.generate_thumbnail()`.
2. The scanner flags `browser_playable=False` for containers/codecs a browser `<video>` tag generally can't decode: `settings.NON_BROWSER_CONTAINERS` (`.mkv`, `.avi`) and `settings.NON_BROWSER_VCODECS` (`hevc`, `h265`, `mpeg4`, `msmpeg4v3`, `wmv3`).
3. `library/apps.py` also runs this scan automatically in the background every `SCAN_INTERVAL_MINUTES` (default 30, 0 disables) once the real server process is up — it's skipped for management commands, tests, and the reloader's first fork.

**Non-browser playback — two paths:**
- **Live HLS transcode (default, primary path)**: `services.start_hls()` spawns an ffmpeg process per video that transcodes to segmented HLS (`libx264 ultrafast crf23` + AAC) into `HLS_DIR`. Sessions are tracked on the *filesystem* (per-video dir + `ffmpeg.pid`), not in-process, so they're shared correctly across multiple gunicorn workers. A cross-process `flock` gives single-flight semantics, `HLS_MAX_CONCURRENT` caps concurrent transcodes, and `HLS_TTL` reaps idle sessions. `views.hls_playlist`/`hls_segment` serve the `.m3u8`/`.ts` files (waiting briefly for warm-up), and the frontend plays HLS via the vendored `library/vendor/hls.min.js`.
- **Convert to MP4 (secondary, permanent-file path)**: the **Convert to MP4** button triggers `services.start_conversion()`, which runs in a background thread and *always fully re-encodes* both streams (`libx264 crf20` + AAC) — stream-copy was deliberately abandoned because it caused A/V desync from irregular source timestamps. Converted files land in `CONVERTED_DIR` (`converted/`). Progress is polled via `/video/<pk>/convert/status/`.

**SPA navigation:** The frontend is a thin SPA — page links send `X-SPA: 1` headers, and views check `is_spa(request)` to render either the full shell (`library/base.html`) or just a content fragment (`library/_spa.html`). All views share context via `base_ctx()` in `views.py`.

**Persistent user state** (theme, autoplay, resume position) lives in the DB via:
- `Setting` model — key/value store (theme, autoplay, loop)
- `PlaybackState` model — one row per video, updated by `/video/<pk>/progress/` (POST from the player every few seconds)
- `WatchEvent` model — one row per viewing session, feeds the History page
- `VideoNote` model — user-added timestamped notes/bookmarks on a video

**Playlists:** Two kinds — manual `Playlist` (ordered via `PlaylistItem.order`) and `SmartPlaylist` (JSON `rules` evaluated at query time in `SmartPlaylist.get_videos()`). Both support a collage thumbnail via `services.generate_collage_thumbnail()`.

**Subtitles:** `services.list_subtitles()` merges three sources — manually uploaded (`VideoSubtitle` model, via `store_uploaded_subtitle()`), yt-dlp/sidecar files (`.srt`/`.ass`/etc.), and embedded MKV subtitle tracks. `ensure_subtitle_vtt()` converts/caches any of them to WebVTT under `SUBTITLE_DIR` on first request.

**File organizer:** `services.organize_by_mtime()` plans and optionally executes moving videos into `LIBRARY_ROOT/YYYY-MM/DD/` by file mtime. It moves sidecars (`.srt`, `.vtt`, `.ass`, `.info.json`, etc.) with the video and updates `file_path`/`rel_path` in the DB so history survives.

**Streaming:** `/stream/<pk>/` in `views.py` handles HTTP Range requests manually (regex `RANGE_RE`, chunked `StreamingHttpResponse`) to support seeking and 4K files without loading everything into memory. It serves the converted copy when one exists.

**PWA:** `views.pwa_manifest` and `views.pwa_icon(size)` generate the manifest and PNG app icons (play-triangle) in pure Python (`struct`/`zlib`, no image library).

## Production deployment

Gunicorn on `127.0.0.1:8002` behind nginx terminating TLS on port 8443. The included `nginx-homeflix.conf` handles the proxy — `proxy_buffering off`, `proxy_request_buffering off`, and long `proxy_read/send_timeout` values are required for Range streaming and HLS to work correctly, not just performance tuning. Set `HOMEFLIX_ORIGINS` so Django's CSRF middleware accepts the HTTPS origin. See README for the full systemd unit.
