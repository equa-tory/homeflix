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

System dependency: `ffmpeg` and `ffprobe` must be on PATH (`apt install ffmpeg`). The only Python dependency is `Django>=5.1` (`requirements.txt`); production adds `gunicorn`. Everything else (fuzzy search, PNG icon generation, HLS session locking) is stdlib.

**Windows:** `run-homeflix.bat` is a self-contained launcher — it creates `.venv`, installs `requirements.txt`, downloads a static ffmpeg build to `%USERPROFILE%\Documents\ffmpeg` if none is on PATH, then runs `migrate` + `runserver`. Edit the `HOMEFLIX_LIBRARY`/`PORT` variables at the top of the file rather than passing env vars. `gunicorn` doesn't work on Windows, so there is no production-server path there — `runserver` is it.

`library/tests.py` covers the auth/permission/per-user-state surface (§ Authentication below) — run a single one with `python manage.py test library.tests.<TestClass>.<test_method>`. It pins `LIBRARY_ROOT` to an empty scratch dir via `@override_settings` so it never scans a real `HOMEFLIX_LIBRARY` the developer's shell happens to have set.

## Architecture

Django project package is `config/` (`config.settings`, `config.urls`, `config.wsgi:application`), with a single app `library/` holding all models/views/services.

**Key environment variables** (set in shell or systemd unit):
- `HOMEFLIX_LIBRARY` — root folder scanned recursively for video files
- `HOMEFLIX_ORIGINS` — comma-separated HTTPS origins for CSRF when behind nginx (e.g. `https://192.168.1.50:8443`)
- `HOMEFLIX_REMOTE_ROOT` — UNC path shown under the player so you can jump to the file from another machine (`REMOTE_ROOT` setting)
- `HOMEFLIX_SECRET_KEY` — Django `SECRET_KEY`. Required for any real deployment; the fallback in `config/settings.py` is a public dev-only placeholder
- `HOMEFLIX_DEBUG` — set `1` for local dev (tracebacks, Django serving `/static/`). Defaults unset (`DEBUG=False`) — leave it that way for anything reachable off your own machine
- `HOMEFLIX_HOSTS` — comma-separated allowed hostnames/IPs (`ALLOWED_HOSTS`). Required once `DEBUG=False`
- `HOMEFLIX_HTTPS` — set `1` only once nginx is actually terminating TLS in front of you; marks session/CSRF cookies `Secure`
- `HOMEFLIX_PUBLIC` — set `1` to let anonymous visitors browse/search/watch/stream without an account (`settings.PUBLIC_ACCESS`). See "Authentication and roles" below for exactly what that does and doesn't exempt

**Directories created at startup** (in `settings.py`, all outside `LIBRARY_ROOT` so the scanner never re-imports them): `thumbnails/`, `converted/`, `hls/`, `subtitles/`, `cache/` (login-throttle counters), `staticfiles/` (only populated by `collectstatic`).

**Authentication and roles:** `django.contrib.auth.middleware.LoginRequiredMiddleware` makes every view default-deny — new views need no extra work, but a view that must be reachable *without* a session (there are currently exactly two unconditionally: `pwa_manifest`, `pwa_icon`) needs an explicit `@login_not_required`. On top of that, `library/auth.py`'s `owner_required` decorator draws a second line: `is_staff` accounts ("owner") can do anything; everyone else ("viewer") is blocked from every view that touches the filesystem or the library catalog — scan, organize, rename, delete, convert, hide, thumbnail regen, and the Manage menu's actions. When adding a new view, ask "does this write to disk or change what videos exist" — if yes, decorate it `@owner_required` (stacked *below* `@require_POST` so the auth check runs first) and hide its UI behind `{% if user.is_staff %}` (server templates) or `data.is_owner` (the JSON payload `_build_watch_data` sends the player). Session/CSRF cookies are namespaced (`homeflix_sessionid` / `homeflix_csrftoken`, `config/settings.py`) because browsers scope cookies by host, not port — Django's default names get overwritten by any other Django app on the same machine, which silently logs you out. The frontend reads the CSRF cookie through `getCookie()`/`CSRF_HDR()` in `base.html`; don't cache the token in a constant. `library/auth.py` also holds `ThrottledLoginView` (per-IP lockout via the filesystem cache) and `login.html` is a standalone page — it does **not** extend `base.html`, since the shell is the SPA itself.

A third, optional tier sits below viewer: with `HOMEFLIX_PUBLIC=1` (`settings.PUBLIC_ACCESS`), `library/auth.py`'s `public_when_enabled` decorator exempts a fixed whitelist of read-only views (browsing/search/watch/stream/shorts/playlists, plus the ephemeral prefs — theme/autoplay/repeat/shuffle/seek-step — and `save_progress`) from login. It's applied once per view, at *module import time* — same evaluate-once-at-startup behavior as every other `HOMEFLIX_*` env var, which is also why `override_settings(PUBLIC_ACCESS=...)` can't toggle already-imported views in a test (see `PublicAccessDecoratorTests` in `library/tests.py`, which tests the decorator directly instead). Anything that mutates shared library data (favorite, rating, playlists, notes) stays login-required even in public mode.

**Per-user state:** `PlaybackState` and `WatchEvent` carry a nullable `user` FK (nullable only so pre-auth rows never block the migration — every real query filters `user=`, so a NULL row is just invisible). Any queryset joining `playback_states` must put every condition — `user=`, `finished=`, etc. — in a single `.filter()`/`.exclude()` call; splitting them across calls makes Django emit separate joins that can each match a *different* user's row. Server-rendered card grids (`_card.html`) can't do that join generically, so views call the `_attach_playback(videos, user)` helper (near `base_ctx` in `views.py`) to stick each video's own-user resume row on as `.my_playback` before rendering — `_card.html` reads `video.my_playback|first|pct:video`, never `video.playback` (that related name no longer exists). Preferences that used to live in the global `Setting` model (theme, autoplay, repeat, shuffle, seek_step) moved to `UserPref` (same `get`/`set` classmethod shape, plus a `user` argument) for the same reason — `default_thumb_percent` is the one preference that *stays* on `Setting`, because `services.scan_library()`'s background thread reads it with no request/user in scope.

**`request.user` vs. `AnonymousUser`:** with `HOMEFLIX_PUBLIC=1` some of the above views run for anonymous visitors. `AnonymousUser` is not a real `User` row, so handing it to a `PlaybackState`/`WatchEvent`/`UserPref` query crashes (`TypeError`/`ValueError` — confirmed directly against these models while building this). `views._state_user(request)` returns the real user or `None` (Django's ORM treats `user=None` as "no such user" for these nullable FKs, which is what lets a read query degrade to "nothing personalized" instead of erroring); it's used *only* at the handful of sites building one of those queries, never as a blanket rename of `request.user` — several of the same functions also check `.is_staff`/`.is_authenticated`, which work fine on the real `AnonymousUser` and would break on `None`. `UserPref.get`/`.set` and `_attach_playback` have their own internal anonymous guards for the same reason. Any new per-user-state code path needs the same treatment — see `AnonymousSafeQueryTests` in `library/tests.py`, which calls views directly with a `RequestFactory` + `AnonymousUser()` request (bypassing the login-required dispatch entirely) specifically to catch this class of bug.

**Data flow for a new video:**
1. `services.scan_library()` walks `LIBRARY_ROOT`, calls `ffprobe` via `services.probe()`, reads optional yt-dlp `.info.json` sidecars via `services.read_sidecar()`, creates `Video` rows (re-probing only when mtime changed), and generates a thumbnail at 0% via `services.generate_thumbnail()`.
2. The scanner flags `browser_playable=False` for containers/codecs a browser `<video>` tag generally can't decode: `settings.NON_BROWSER_CONTAINERS` (`.mkv`, `.avi`) and `settings.NON_BROWSER_VCODECS` (`hevc`, `h265`, `mpeg4`, `msmpeg4v3`, `wmv3`).
3. `library/apps.py` also runs this scan automatically in the background every `SCAN_INTERVAL_MINUTES` (default 30, 0 disables) once the real server process is up — it's skipped for management commands, tests, and the reloader's first fork.

**Non-browser playback — two paths:**
- **Live HLS transcode (default, primary path)**: `services.start_hls()` spawns an ffmpeg process per video that transcodes to segmented HLS (`libx264 ultrafast crf23` + AAC) into `HLS_DIR`. Sessions are tracked on the *filesystem* (per-video dir + `ffmpeg.pid`), not in-process, so they're shared correctly across multiple gunicorn workers. A cross-process `flock` gives single-flight semantics, `HLS_MAX_CONCURRENT` caps concurrent transcodes, and `HLS_TTL` reaps idle sessions. `views.hls_playlist`/`hls_segment` serve the `.m3u8`/`.ts` files (waiting briefly for warm-up), and the frontend plays HLS via the vendored `library/vendor/hls.min.js`.
- **Convert to MP4 (secondary, permanent-file path)**: the **Convert to MP4** button triggers `services.start_conversion()`, which runs in a background thread and *always fully re-encodes* both streams (`libx264 crf20` + AAC) — stream-copy was deliberately abandoned because it caused A/V desync from irregular source timestamps. Converted files land in `CONVERTED_DIR` (`converted/`). Progress is polled via `/video/<pk>/convert/status/`.

**SPA navigation:** The frontend is a thin SPA — page links send `X-SPA: 1` headers, and views check `is_spa(request)` to render either the full shell (`library/base.html`) or just a content fragment (`library/_spa.html`). All views share context via `base_ctx()` in `views.py`.

**Search:** `views._search_filter` matches in Python (`casefold()`, separator-insensitive, all words ANDed) over title, filename, channel and tag names — SQLite `icontains` can't case-fold non-ASCII. Typo-tolerant fuzzy fallback only when nothing matches. The header search box lives outside `#app`, so `ROUTER.search()`/`syncSearchBox()` keep it in step with `?q=`; `ROUTER.go` drops superseded responses (`navSeq`). The spatial-nav key handler must never handle Enter/letters/caret-arrows while an input is focused.

**Seek step:** `_build_watch_data` sends `seek_step: null` until the account has saved one, so the client keeps (and uploads once) its localStorage value rather than a server default overwriting it.

**Persistent user state** lives in the DB via:
- `Setting` model — global key/value store (currently just `default_thumb_percent`)
- `UserPref` model — per-user key/value store (theme, autoplay, repeat, shuffle, seek_step)
- `PlaybackState` model — one row per (video, user), updated by `/video/<pk>/progress/` (POST from the player every few seconds)
- `WatchEvent` model — one row per (video, user) viewing session, feeds the History page
- `VideoNote` model — user-added timestamped notes/bookmarks on a video (shared, not per-user — any logged-in account can add/see/delete a video's notes)

**Playlists:** Two kinds — manual `Playlist` (ordered via `PlaylistItem.order`) and `SmartPlaylist` (JSON `rules` evaluated at query time in `SmartPlaylist.get_videos()`). Both support a collage thumbnail via `services.generate_collage_thumbnail()`.

**Subtitles:** `services.list_subtitles()` merges three sources — manually uploaded (`VideoSubtitle` model, via `store_uploaded_subtitle()`), yt-dlp/sidecar files (`.srt`/`.ass`/etc.), and embedded MKV subtitle tracks. `ensure_subtitle_vtt()` converts/caches any of them to WebVTT under `SUBTITLE_DIR` on first request.

**File organizer:** `services.organize_by_mtime()` plans and optionally executes moving videos into `LIBRARY_ROOT/YYYY-MM/DD/` by file mtime. It moves sidecars (`.srt`, `.vtt`, `.ass`, `.info.json`, etc.) with the video and updates `file_path`/`rel_path` in the DB so history survives.

**Streaming:** `/stream/<pk>/` in `views.py` handles HTTP Range requests manually (regex `RANGE_RE`, chunked `StreamingHttpResponse`) to support seeking and 4K files without loading everything into memory. It serves the converted copy when one exists.

**PWA:** `views.pwa_manifest` and `views.pwa_icon(size)` generate the manifest and PNG app icons (play-triangle) in pure Python (`struct`/`zlib`, no image library).

**Mobile safe-area gotcha:** `--bottom-nav-h` (`base.html`'s mobile media query) is what `#bulkBar`/`#reorderBar` sit `bottom:` above the tab bar with — it must equal the tab bar's *true* on-screen height, i.e. `calc(<content height> + env(safe-area-inset-bottom,0px))`, not a bare pixel value. A bare-pixel `--bottom-nav-h` is exactly what put those bars behind/overlapping the tab bar on notched iPhones (Safari, normal tab — not installed-to-homescreen). Any new `position:fixed` element anchored to the bottom of the screen needs the same `env(safe-area-inset-bottom)` treatment.

**Shorts mode:** any portrait video (`data.is_portrait`, not just ones opened from the Shorts page) auto-enters `.ph-shorts` mode in the player (`applyData()`, `base.html`), which hides `#phInfo` (the favorite/rating/hide/delete/playlist/notes/rename/convert panel `renderInfo()` builds) entirely — there's no room for it in the swipe-feed layout. `renderShortsRail()`/`bindShortsRail()` (`base.html`, right after `bindInfo()`) render a compact stand-in: favorite, rating (popover), add-to-playlist (popover), and a `•••` overflow for the owner-only actions plus a quick add-note field, reusing the same `.bulk-aw`/`.bulk-pop` popover chrome (and its page-wide outside-click-closes-popover listener) the bulk-select bar already has. Called alongside `renderInfo(data)` on every video load; which one is actually visible is CSS-only (`#player.ph-expanded.ph-shorts #phShortsRail`/`#phInfo`), so the two can never desync.

**Playlist downloader** (`library/downloader.py`, page `downloads.html`, Manage menu → "Download from playlist…"): a port of the sibling Lasso project's yt-dlp wrapper (`build_cmd`, archive file, `--print-to-file` success markers) as an owner-only web page. Everything — read endpoints included — is `@owner_required`, since it writes into the library and stores YouTube credentials. Flow: `DownloadSource` (saved link + optional `target_subdir`, validated by `resolve_target()` to stay inside `LIBRARY_ROOT`) → `fetch_playlist()` (`yt-dlp --flat-playlist -J`, cached in `entries_json`) → `classify()` each entry as new / downloaded / skipped / archived / unavailable → owner ticks rows → `start_job()` runs yt-dlp in a daemon thread. Things that aren't obvious:
- **Comparison is by name**, via `name_key()` (NFKC + casefold + strip everything non-alphanumeric), because yt-dlp sanitises `: ? " | /` differently across versions/platforms. `local_index()` walks the target folder *recursively* (Organize moves files into `YYYY-MM/DD/`) and also uses `Video.title`/`source_url` rows from the sidecar. "Mark downloaded" is a DB row (`SkippedEntry`) only — Lasso's archive is never written to by it.
- **Lasso archive is shared**: `archive_path()` is `_yt_archive_<md5(url)[:8]>.txt` in the target folder, exactly Lasso's name, so the URL is stored in canonical `playlist?list=` form (`normalize_url()`; only YouTube hosts accepted). Before a run, the explicitly selected ids are removed from it (`forget_in_archive`) so re-selecting an "archived" row really re-downloads.
- **Cookies are pasted, not read from a browser** (the server has none, and the point is phone use). `save_cookies()` accepts a Netscape `cookies.txt` or a raw `Cookie:` header, stores it 0600 in `settings.YTDL_DIR` (outside the library), and nothing ever sends it back to the client — the UI only learns set/count/date. Downloads use the real file (yt-dlp writes rotated cookies back to it); `fetch_playlist()` uses a temp *copy* so a fetch can't race a running download. With cookies, `player_client=web_embedded,default` (see Lasso's CLAUDE.md for the SABR/DASH reason).
- **Job state is in the DB + the yt-dlp pid**, not memory, so status polling/cancel work from any gunicorn worker (same idea as HLS). Success is judged by the `after_move` marker file, never yt-dlp's exit code (`--ignore-errors` makes it unreliable). `reap_stale()` fails a job whose pid died (server restart) with conditional `UPDATE`s so it can't overwrite a thread that finished at the same instant. One job at a time. The post-download `scan_library()` runs *after* the job is marked done.
- `_run_job(job_id, close_db=False)`: only the real thread passes `close_db=True`; tests call it directly inside their transaction. Tests never run real yt-dlp — they patch `fetch_playlist`/`start_job`, or `build_cmd` to a stand-in process.
- yt-dlp is a pip dependency but needs Deno/Node on the server to solve YouTube's n-challenge (`js_runtime()`); the page warns if either is missing, and has an "Update yt-dlp" button (`start_update`, pip in a thread, status in `YTDL_DIR/update.json`).

## Production deployment

Gunicorn on `127.0.0.1:8002` behind nginx terminating TLS on port 8443. The included `nginx-homeflix.conf` handles the proxy — `proxy_buffering off`, `proxy_request_buffering off`, and long `proxy_read/send_timeout` values are required for Range streaming and HLS to work correctly, not just performance tuning. Set `HOMEFLIX_ORIGINS` so Django's CSRF middleware accepts the HTTPS origin. See README for the full systemd unit.
