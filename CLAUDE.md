# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

HomeFlix is a self-hosted, single-user video library built with Django + SQLite. It streams video from a local folder to any device on the LAN (PC, phone, LG TV). There is no authentication — it's designed for a trusted local network.

## Commands

```bash
# Set the video library path (required)
export HOMEFLIX_LIBRARY=/path/to/your/videos

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

System dependency: `ffmpeg` and `ffprobe` must be on PATH (`apt install ffmpeg`).

## Architecture

Single Django app (`library/`) with no external Python dependencies beyond Django itself.

**Key environment variables** (set in shell or systemd unit):
- `HOMEFLIX_LIBRARY` — root folder scanned recursively for video files
- `HOMEFLIX_ORIGINS` — comma-separated HTTPS origins for CSRF when behind nginx (e.g. `https://192.168.1.50:8443`)

**Data flow for a new video:**
1. `services.scan_library()` walks `LIBRARY_ROOT`, calls `ffprobe` via `services.probe()`, reads optional yt-dlp `.info.json` sidecars via `services.read_sidecar()`, creates `Video` rows, and generates a thumbnail at 0% via `services.generate_thumbnail()`.
2. The scanner flags `browser_playable=False` for MKV containers and HEVC/H.265 codecs (see `settings.NON_BROWSER_CONTAINERS`, `NON_BROWSER_VCODECS`).
3. Non-browser-playable videos show a **Convert to MP4** button. `services.start_conversion()` spawns a background thread that runs ffmpeg, copying H.264/AAC streams or re-encoding only what's needed. Converted files land in `converted/` (outside `LIBRARY_ROOT` so they're never re-scanned). Progress is polled via `/video/<pk>/convert/status/`.

**SPA navigation:** The frontend is a thin SPA — page links send `X-SPA: 1` headers, and views check `is_spa(request)` to render either the full shell (`library/base.html`) or just a content fragment (`library/_spa.html`). All views share context via `base_ctx()` in `views.py`.

**Persistent user state** (theme, autoplay, resume position) lives in the DB via:
- `Setting` model — key/value store (theme, autoplay preference)
- `PlaybackState` model — one row per video, updated by `/video/<pk>/progress/` (POST from the player every few seconds)
- `WatchEvent` model — one row per viewing session, feeds the History page

**Playlists:** Two kinds — manual `Playlist` (ordered via `PlaylistItem.order`) and `SmartPlaylist` (JSON rules evaluated at query time in `SmartPlaylist.get_videos()`).

**File organizer:** `services.organize_by_mtime()` plans and optionally executes moving videos into `LIBRARY_ROOT/YYYY-MM/DD/` by file mtime. It moves sidecars (`.srt`, `.vtt`, `.ass`, `.info.json`, etc.) with the video and updates `file_path`/`rel_path` in the DB so history survives.

**Streaming:** `/stream/<pk>/` in `views.py` handles HTTP Range requests manually (regex `RANGE_RE`, chunked `StreamingHttpResponse`) to support seeking and 4K files without loading everything into memory.

**Directories created at startup** (in `settings.py`): `thumbnails/` and `converted/` — both outside `LIBRARY_ROOT`.

## Production deployment

Gunicorn on `127.0.0.1:8002` behind nginx terminating TLS on port 8443. The included `nginx-homeflix.conf` handles the proxy. Set `HOMEFLIX_ORIGINS` so Django's CSRF middleware accepts the HTTPS origin. See README for full systemd unit.
