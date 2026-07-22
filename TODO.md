# TODO / ideas

Deferred feature ideas, not yet built. Kept here so they don't get lost.

## Playlist "presentation" / summary video

A button on a playlist (and smart playlist) page that auto-generates one
summary video made of random short clips from the videos it contains.

Suggested approach, following existing patterns in `library/services.py`:
- For N random videos in the playlist, cut a short clip from each with
  `ffmpeg -ss <random offset> -t <clip length> -i <file> ...` (same `-ss`
  seek pattern as `generate_thumbnail`).
- Concatenate the clips with a `filter_complex concat`, normalizing each
  input first with `scale`+`setsar` (the collage helper
  `generate_collage_thumbnail` already works around mismatched-input crashes
  the same way — reuse that pattern rather than `hstack`/`vstack`).
- Run it as a background thread, modeled on `start_conversion()` /
  `_ffmpeg_convert()` — same `-progress pipe:1 -nostats` progress parsing,
  same cancel-via-`terminate()` mechanism, output written to `CONVERTED_DIR`
  (or a new `SUMMARIES_DIR`).
- UI: a "🎬 Generate summary" button + progress poll, mirroring the existing
  Convert to MP4 button/status endpoint on the watch page.

## Global random-vertical side panel

A small toggle available on every page (not just Shorts) that instantly
opens a side panel playing a random portrait ("shorts") video, with swipe/
scroll-down to advance to another random one — without leaving the current
page. Would reuse the vertical shorts player mode (`ph-shorts`) but in a
docked panel instead of the full expanded player.
