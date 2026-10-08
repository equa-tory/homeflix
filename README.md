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

## Downloading from a YouTube playlist (owner only)

**Manage (⋯) → ⬇ Download from playlist…** — works from a phone, everything runs on the server.

1. Save a playlist link (optionally into a subfolder of the library). Several links can be saved.
2. **Fetch list.** The playlist is compared **by name** against the target folder (all
   subfolders, so Organize-by-date doesn't confuse it). Videos you don't have are listed and
   pre-ticked; untick the ones you don't want, or use the range box (`3-10`).
3. **Mark downloaded** on a row if you already have it under another name — it's remembered
   and never offered again (**Unskip** undoes it).
4. **Download**. Progress shows live; finished files appear in the library right away.

The bar at the bottom shows a rough size for what you've ticked (based on the average size
per minute of the videos already in your library) next to the free space on the target disk,
and warns before you start something that won't fit. It's an estimate, not an exact figure.

**You must paste your YouTube cookies first** (the box at the top of the page) — downloading
is switched off without them, since YouTube otherwise caps quality and blocks with "Sign in to
confirm you're not a bot". They're stored on the server (mode 0600, never shown again) and
used for every download, so you only do it once — the page has the step-by-step (use a
private window and close it without signing out, so YouTube doesn't rotate them). Cookies
expire eventually; paste fresh ones if downloads start failing with a sign-in error.

**Needs Deno 2.3+ (or Node 22+, or Bun 1.2.11+) on the server.** yt-dlp silently ignores older
runtimes (e.g. the Node 18 many distros ship), after which *every* download fails with "n
challenge solving failed / The page needs to be reloaded". The page checks the version and
tells you; `./install.sh` (below) installs Deno into the project folder for you, no root needed.

**Update library from YouTube** (card further down the same page): refreshes the videos you
already have. Each library video is matched **by name** against your saved playlists and gets
its author/channel and thumbnail (and, optionally, YouTube's title) updated — one playlist
fetch covers every video in it, no request per video. Shorts keep their own frame as
thumbnail; videos that aren't in any saved playlist are left alone.

It shares its `_yt_archive_*.txt` file with the Lasso downloader in the same folder,
so the two tools never re-download each other's videos. YouTube breaks yt-dlp regularly — use
the **Update yt-dlp** button on the page when everything suddenly fails.

## Channels, playlists and the player
- **Channels** tab: every channel with a collage of its newest videos' thumbnails, its name and how many videos it has; click one to see its videos (with sort, select, 🎲 random scoped to that channel). 🎲 *Random channel* opens a random one. Videos only get a channel when downloaded with metadata or after **Download from playlist → Update library from YouTube**.
- **Smart playlists** can match **ALL** rules (and) or **ANY** rule (or) — pick it at the top of the rules.
- On a playlist page the **cover image** and (smart playlists) **conditions** live under a collapsed **⚙ Playlist settings**. The sort you choose is remembered per playlist (and per account).
- In the player the file path, thumbnail regeneration and rename are tucked under a collapsed **🛠 Tools**.

## Backups (owner only)

**Manage (⋯) → 💾 Backups…** — automatic backups of HomeFlix's own data. Defaults: every
**48 hours** into **`/mnt/ssd/backups/homeflix`**, keeping **1** backup (a new one replaces the
old only after it succeeded). One archive `homeflix-YYYYMMDD-HHMMSS.tar.gz` holds a consistent
snapshot of the database (favorites, ratings, playlists, notes, history, accounts, saved download
playlists), `thumbnails/` (incl. uploaded playlist covers) and `subtitles/`. **Not** included: your
videos, `converted/`, and secrets (`homeflix.env`, saved YouTube cookies). Only files named
`homeflix-YYYYMMDD-HHMMSS.tar.gz` are ever created or deleted in that folder.

Restore: stop the service, `tar -xzf homeflix-….tar.gz -C /tmp/hf-restore`, copy `db.sqlite3`,
`thumbnails/` and `subtitles/` back into the HomeFlix folder, start the service.

## Playback notes
- Shorts (vertical videos) and anything under 3 minutes always start from the beginning: no
  resume point, no progress bar, not in "Continue watching".
- The 🎲 button under ☑ Select (left edge of Library/Playlists) opens a random video from what
  you're looking at (current search/filter or playlist).

## Requirements
- Python 3.10+, `pip install -r requirements.txt` (Django + yt-dlp)
- ffmpeg + ffprobe on PATH (`sudo apt install ffmpeg`)
- For the playlist downloader: [Deno](https://deno.com) 2.3+ (or Node 22+) — yt-dlp needs a
  *recent* JS runtime to solve YouTube's challenge (`./install.sh` sets Deno up for you)

## Easy install on Linux (systemd service)

```bash
./install.sh
```
Run it as your normal user (it uses `sudo` only where needed). It asks for your video folder,
then: installs missing system packages (ffmpeg, …) via apt, creates the virtualenv and installs
the dependencies, installs **Deno** into `./.deno` if there's no JS runtime new enough for
yt-dlp, writes `homeflix.env` (settings + a generated secret key, mode 600), migrates the
database, creates your owner login, and installs + starts a `homeflix` **systemd service** that
comes back after a reboot. At the end it prints the URLs to open on your phone / TV.

- Non-interactive: `./install.sh --yes --library /mnt/videos` (also `--port`, `--bind`, `--user`).
- `--bind 127.0.0.1` keeps it off the network, for use behind nginx (next section).
- Settings live in `homeflix.env` — edit, then `sudo systemctl restart homeflix`. Logs:
  `journalctl -u homeflix -f`.
- Safe to re-run after a `git pull` (keeps your settings and database, then restarts).
- Already have a hand-written `homeflix.service` from the section below? The installer offers to
  import its `HOMEFLIX_*` settings, and never replaces a service it didn't create without asking
  (it saves a `.bak` copy first). If it had no `HOMEFLIX_SECRET_KEY`, one is generated.
- `./install.sh --no-service` does everything except systemd; `./install.sh --uninstall` removes
  the service (your videos, database and settings are left alone). The unit template is
  `deploy/homeflix.service.in`.
- Windows: use `run-homeflix.bat` instead.

## Quick start (development)
```bash
export HOMEFLIX_LIBRARY=/path/to/your/videos
python manage.py migrate
python manage.py createsuperuser   # your owner login
python manage.py scan              # or press "Rescan" in the Manage (⋯) menu
python manage.py runserver 0.0.0.0:8002
```
Open `http://<server-ip>:8002/` and sign in. `HOMEFLIX_LIBRARY` is the folder used until you
change it: **⋯ → Library folders…** lets you add any number of folders (another drive, a second
share) and mark one as the *main* folder where downloads are saved. All of them are scanned and
watched as one library; nothing has to be moved.

 `HOMEFLIX_DEBUG=1` enables Django's
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
   ExecStart=/path/to/venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8002 --workers 3 --worker-class gthread --threads 8 --timeout 120
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```
   ```bash
   sudo systemctl daemon-reload && sudo systemctl enable --now homeflix
   ```
   - **`--worker-class gthread --threads 8`, not the default sync workers.** A video
     stream keeps its connection open for as long as you watch; with sync workers each
     stream pins a whole worker (three viewers freeze the site) and gunicorn kills a worker
     that serves a single response for longer than `--timeout` — which showed up as videos
     randomly stopping mid-play (`WORKER TIMEOUT … GET /stream/…` in `journalctl`).
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
