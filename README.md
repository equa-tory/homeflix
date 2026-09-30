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

**Accounts** — everyone signs in. There are two roles:
- **Owner** (`is_staff`) — everything, including scan/organize/convert/rename/
  delete and the Manage menu. Create yours with `createsuperuser` (below).
- **Viewer** — browse, search, stream, resume, favorite, rate, use and create
  playlists, add notes. Blocked from anything that touches files or the
  catalog (no Manage menu, no delete/rename/organize/convert, no Hidden page).
  Create one in `/admin` → *Users* → *Add* (leave "Staff status" unchecked).

Resume position, watch history, and player preferences (theme, autoplay,
repeat, shuffle, seek step) are private per account.

**Public read-only mode** — set `HOMEFLIX_PUBLIC=1` to let anyone browse,
search, and watch without signing in at all (a "YouTube, not Fort Knox" mode
for casually sharing the link). A **Log in** link stays in the top right for
you (and friends) to get an account and its extras. Anything that writes
shared data (favorite, rating, playlists, notes) or touches the filesystem/
catalog still requires an account — an anonymous visitor gets sent to
`/login/` the moment they try. Off by default; needs a process restart
(like every other `HOMEFLIX_*` variable) to take effect.

## Requirements
- Python 3.10+, `pip install -r requirements.txt` (Django only)
- ffmpeg + ffprobe on PATH (`sudo apt install ffmpeg`)

## Quick start (development)
```bash
export HOMEFLIX_LIBRARY=/path/to/your/videos
python manage.py migrate
python manage.py createsuperuser   # your owner login
python manage.py scan              # or press "Rescan" in the Manage (⋯) menu
python manage.py runserver 0.0.0.0:8002
```
Open `http://<server-ip>:8002/` and sign in. `HOMEFLIX_DEBUG=1` enables Django's
debug pages and its built-in static file serving for `/admin` — leave it unset
(the default, `DEBUG=False`) for anything reachable outside your own machine.

## Run behind nginx with HTTPS (port 8443)

1. Generate a real secret key (the one in `config/settings.py` is a
   dev-only placeholder — never use it once this is reachable off your own
   machine):
   ```bash
   python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"
   ```

2. Collect static files (needed once `DEBUG=False`, mainly so `/admin` has its
   CSS — Django's dev server stops serving `/static/` itself):
   ```bash
   python manage.py collectstatic --noinput
   ```

3. Serve the app on 127.0.0.1:8002 with gunicorn (survives reboots via systemd):
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
   Environment="HOMEFLIX_HOSTS=192.168.1.50"
   Environment="HOMEFLIX_HTTPS=1"
   Environment="HOMEFLIX_SECRET_KEY=paste-the-generated-key-here"
   ExecStart=/path/to/venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8002 --workers 3 --timeout 120
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```
   ```bash
   sudo systemctl daemon-reload && sudo systemctl enable --now homeflix
   ```
   - **`--bind 127.0.0.1:8002`, not `0.0.0.0:8002`.** Binding to `0.0.0.0` makes
     gunicorn directly reachable from the network, bypassing nginx (and its TLS,
     and — once collectstatic/the nginx static block below are set up — its
     `/static/` handling) entirely. Only nginx should be reachable from
     outside; gunicorn should only ever be reachable from nginx on the same box.
   - `HOMEFLIX_ORIGINS` — your HTTPS URL(s), so POST/CSRF works behind the proxy.
   - `HOMEFLIX_HOSTS` — hostnames/IPs this server answers to (Django rejects
     unrecognized `Host` headers once `DEBUG=False`).
   - `HOMEFLIX_HTTPS=1` — marks session/CSRF cookies `Secure`. Only set this
     once nginx is actually terminating TLS in front of you (see below) —
     otherwise the login cookie won't be sent and you can't sign in.

4. Add the nginx site (file included: `nginx-homeflix.conf`):
   ```bash
   sudo cp nginx-homeflix.conf /etc/nginx/sites-available/homeflix
   sudo ln -s /etc/nginx/sites-available/homeflix /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   ```
   Browse to `https://<server-ip>:8443/`. (Open-WebUI keeps port 443 — nginx
   allows only one `default_server` per port, so HomeFlix uses 8443.) The
   included config also serves `/static/` directly from `staticfiles/` — update
   the path there if you didn't run `collectstatic` from this checkout.

## "/admin has no CSS" (and toggling HOMEFLIX_DEBUG doesn't fix it)
This is expected, not a bug to chase with `DEBUG`: **gunicorn never serves
`/static/` itself, on any `DEBUG` setting.** Only Django's own `runserver`
does that (and only with `DEBUG=1`, or `--insecure`). Once you're running
gunicorn+nginx, styling `/admin` needs all three of:
1. `python manage.py collectstatic --noinput` has actually been run.
2. `nginx-homeflix.conf`'s `location /static/ { alias ...; }` path matches
   your real checkout (a stale/placeholder path here 404s silently).
3. You're browsing the **nginx** URL (`https://<host>:8443/admin`), not
   gunicorn directly (`http://<host>:8002/admin`) — and per the `--bind`
   note above, the direct gunicorn URL shouldn't even be reachable from
   another machine once it's bound to `127.0.0.1`.

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

`/admin/` (sign in with your owner account) also bulk-edits titles, tags, etc.
