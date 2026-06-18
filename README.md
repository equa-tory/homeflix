# HomeFlix — your personal local video site

A minimal, self-hosted "YouTube for one" built with Django + SQLite.
Open it from your PC, phone, and LG TV at the same URL — all state (resume,
history, favorites, ratings, playlists, theme) lives in one database on the
server, so everything stays in sync automatically.

## Features

**Library & playback**
- Recursive scan (mp4 / webm / mkv / mov / m4v / avi). Non-video files (txt, png,
  jpeg …) are ignored and never touched.
- Auto thumbnails via ffmpeg (default at 0%; regenerate from any % on the watch page).
- ffprobe metadata: duration, resolution, codecs, size, quality badge.
- yt-dlp sidecar import: a matching `.info.json` fills in real title / channel / URL / date.
- **Custom player** with HTTP Range streaming (seeking + 4K friendly), resume,
  buffered bar, volume, fullscreen, prev/next, and auto-advance on end.
- Search, sort (date added / name / duration / modified), Recently added,
  Continue watching, watch history, favorites, 1–5 star ratings, tags, playlists.
- Dark / light theme, remembered server-side.

**Plays MKV / HEVC** — files a browser can't play natively show a one-click
**Convert to MP4** button. It copies streams it can keep (fast, lossless for
H.264/AAC) and only re-encodes what the browser can't handle. The converted copy
is stored separately; your original file is never modified. (Your LG TV often
plays the original directly anyway.)

**TV remote control** — the whole UI is drivable with just arrows + OK:
- Grids/menus: arrow keys move the highlight, **OK** opens, **Back** goes back.
- On the player: **OK** play/pause · **◀ ▶** seek 10s · **▲** next video ·
  **▼** previous video · **Back** returns to the library. Media keys
  (play/pause, ⏪ ⏩, ⏮ ⏭) work too.

**File organizer** — *Manage → Organize by date* moves each video into
`LIBRARY_ROOT/YYYY-MM/DD/` (e.g. `2026-06/18/`) based on its modified date.
Subtitles/sidecars move with it; unrelated files stay put. Shows a **dry-run
preview** before anything moves, and updates the database in place so your watch
history survives the move.

**Library maintenance** (Manage menu): Rescan · Remove missing files ·
Reset & rebuild (clears records and rescans — files on disk are never deleted).

## Requirements
- Python 3.10+, `pip install Django`
- ffmpeg + ffprobe on PATH (`sudo apt install ffmpeg`)

## Quick start (development)
```bash
export HOMEFLIX_LIBRARY=/path/to/your/videos
python manage.py migrate
python manage.py scan          # or press "Rescan" in the Manage (⋯) menu
python manage.py runserver 0.0.0.0:8002
```
Open `http://<server-ip>:8002/`.

## Run behind nginx with HTTPS (port 8443)

1. Serve the app on 127.0.0.1:8002 with gunicorn (survives reboots via systemd):
   ```bash
   pip install gunicorn
   ```
   `/etc/systemd/system/homeflix.service`:
   ```ini
   [Unit]
   Description=HomeFlix
   After=network.target

   [Service]
   User=youruser
   WorkingDirectory=/path/to/homeflix
   Environment="HOMEFLIX_LIBRARY=/mnt/videos"
   Environment="HOMEFLIX_ORIGINS=https://192.168.1.50:8443"
   ExecStart=/path/to/venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8002 --workers 3 --timeout 120
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```
   ```bash
   sudo systemctl daemon-reload && sudo systemctl enable --now homeflix
   ```
   Set `HOMEFLIX_ORIGINS` to your HTTPS URL(s) so POST/CSRF works behind the proxy.

2. Add the nginx site (file included: `nginx-homeflix.conf`):
   ```bash
   sudo cp nginx-homeflix.conf /etc/nginx/sites-available/homeflix
   sudo ln -s /etc/nginx/sites-available/homeflix /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   ```
   Browse to `https://<server-ip>:8443/`. (Open-WebUI keeps port 443 — nginx
   allows only one `default_server` per port, so HomeFlix uses 8443.)

## "Some mp4 videos are black on the TV"
That's almost always a codec the TV *browser* can't decode (commonly HEVC/H.265,
10-bit, or HDR) even though the TV's built-in media player can. Open the video on
a PC: if it's also black there, or the watch page shows codec `hevc`, use the
**Convert to MP4** button — it re-encodes to H.264, which plays everywhere. If it
plays fine on PC but is black only on the TV, it's a TV-browser codec gap, and
converting fixes that too. (Rule out the earlier Wi-Fi/VPN routing issue as well.)

## Notes
- Converted copies live in `converted/` (outside your library, so they're never
  re-scanned). Thumbnails live in `thumbnails/`. Both are caches.
- Conversion runs in a background thread; large HEVC files take real time
  (it's a full re-encode). H.264-in-MKV is near-instant (container swap only).
- For very heavy use you can later have nginx serve the files directly via
  X-Accel-Redirect; the current proxy setup is fine for personal use.

`python manage.py createsuperuser` → `/admin/` to bulk-edit titles, tags, etc.
