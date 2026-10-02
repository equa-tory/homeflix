import os
import re
import json
import difflib
import mimetypes

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.db.models import Count, F, Max, Q, Sum
from django.http import (
    StreamingHttpResponse, HttpResponse, JsonResponse, Http404, FileResponse,
    HttpResponseNotModified,
)
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.utils.http import http_date
from django.views.static import was_modified_since

from .auth import owner_required, public_when_enabled
from .models import (
    Video, PlaybackState, WatchEvent, Playlist, PlaylistItem, Tag, Setting,
    UserPref, VideoSubtitle, DownloadSource, SkippedEntry, DownloadJob, RESUME_MIN_SECONDS,
)
from . import services, downloader, backup, refresh

RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")
CHUNK = 8192
MAX_RANGE_BYTES = 32 * 1024 * 1024   # cap for an open-ended Range request, see stream()

# Python's mimetypes module has no entry for these containers, so guess_type()
# falls back to application/octet-stream — iOS Safari's download-attribute
# handling treats "unknown" types differently from recognized video/* types,
# so an unrecognized type is what breaks the download button for these.
EXTRA_MIME_TYPES = {
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".ts": "video/mp2t",
    ".m4v": "video/mp4",
}


# ---- helpers ---------------------------------------------------------------

def theme(request):
    return UserPref.get(request.user, "theme", "dark")


def is_spa(request):
    """True when the SPA router fetched this view for a content fragment."""
    return request.headers.get("X-SPA") == "1"


def base_ctx(request, *, active_nav="", page_id="", spa_title="HomeFlix", **extra):
    ctx = {
        "theme": theme(request),
        "library_root": settings.LIBRARY_ROOT,
        "active_nav": active_nav,
        "page_id": page_id,
        "spa_title": spa_title,
        # default_thumb_percent stays a global Setting (not per-user) --
        # it's also read by the background scanner, which has no user.
        "default_thumb_percent": Setting.get("default_thumb_percent", "0"),
        "is_owner": request.user.is_authenticated and request.user.is_staff,
        # The page templates extend this. Full load -> shell; SPA fetch -> fragment.
        "base_template": "library/_spa.html" if is_spa(request) else "library/base.html",
    }
    ctx.update(extra)
    return ctx


def _state_user(request):
    """The real User for per-(video,user) state (PlaybackState/WatchEvent/
    UserPref queries), or None for an anonymous visitor (HOMEFLIX_PUBLIC
    read-only mode). Passing request.user directly into one of those crashes
    for AnonymousUser -- it isn't a real model instance, so the ORM can
    neither filter nor assign it as a FK value. None is what Django's ORM
    treats as "no such user" for these nullable FKs, which is what lets a
    public/anonymous request degrade to "no personalized state" instead of
    erroring. Do NOT use this for .is_staff/.is_authenticated checks -- those
    work fine on the real request.user and would break on None."""
    u = request.user
    return u if u.is_authenticated else None


def _saved_seek_step(user):
    """The account's saved seek step in seconds, or None if never set (or
    anonymous) -- see the seek_step note in _build_watch_data."""
    raw = UserPref.get(user, "seek_step", "")
    try:
        return max(1, min(600, int(float(raw)))) if raw != "" else None
    except ValueError:
        return None


def _attach_playback(videos, user):
    """Attach `.my_playback` (a 0-or-1-item list holding this user's own
    PlaybackState) to each Video in `videos`, in one extra query. Since
    PlaybackState is now per-(video,user), a plain select_related/reverse
    accessor would match whichever user's row happens to be first -- every
    server-rendered card grid (_card.html reads `video.my_playback|first`)
    needs its videos passed through this before rendering.

    `user` should be the result of _state_user() (a real User or None) --
    but this also tolerates a raw AnonymousUser reaching it by mistake."""
    videos = list(videos)
    if user is None or not user.is_authenticated:
        for v in videos:
            v.my_playback = []
        return videos
    ids = [v.pk for v in videos]
    states = {s.video_id: s for s in PlaybackState.objects.filter(video_id__in=ids, user=user)}
    for v in videos:
        v.my_playback = [states[v.pk]] if v.pk in states else []
    return videos


SORTS = {
    "added": "-date_added",
    "duration": "-duration_seconds",
}

# Same sort choices as Library's SORTS, but for a PlaylistItem queryset (needs
# the video__ prefix) plus "manual" -- the playlist's own insertion order,
# and the default so existing playlists keep behaving exactly as before
# unless the user explicitly picks a different sort.
PLAYLIST_SORTS = {
    "manual": "order",
    "added": "-video__date_added",
    "duration": "-video__duration_seconds",
}

# "Rating" sort is two-key (favorites/hearts first, then star rating) so it
# can't live in the single-string SORTS/PLAYLIST_SORTS dicts above -- these
# are the field pairs used to special-case it in each sort site below.
RATING_ORDER = ("-favorite", "-rating")
RATING_ORDER_REV = ("favorite", "rating")
PLAYLIST_RATING_ORDER = ("-video__favorite", "-video__rating")
PLAYLIST_RATING_ORDER_REV = ("video__favorite", "video__rating")

# Vertical/"shorts" videos. `>=` so square videos count as vertical too.
# Python-side twin: Video.is_portrait -- keep the two in sync.
PORTRAIT_Q = Q(height__gte=F("width"), width__gt=0)


def _apply_sort(order, rev):
    """Flip a '-field'/'field' order string per the rev flag -- shared by
    Library, Playlist, and Smart Playlist sorting."""
    if rev:
        return order[1:] if order.startswith("-") else f"-{order}"
    return order


_SEP_RE = re.compile(r"[-_.\s]+")


_WORD_RE = re.compile(r"\w+")
_FUZZY_THRESHOLD = 0.8
_FUZZY_MIN_LEN = 4


def _squash(text):
    """casefold + drop -, _, ., whitespace -- so 'r-906'/'r_906'/'r 906' all
    match 'r906'. casefold() (unlike SQLite's LIKE, which only folds ASCII)
    handles Cyrillic/accented text too."""
    return _SEP_RE.sub("", text.casefold())


def _search_filter(qs, q):
    """Loose search over title, filename, channel and tag names: case- and
    separator-insensitive, every query word must match (AND). Falls back to
    typo-tolerant fuzzy matching (stdlib difflib) when that finds nothing.

    Done in Python rather than SQL because SQLite can't case-fold non-ASCII;
    a single-user library is small enough that one pass is cheap."""
    tokens = [t for t in (_squash(w) for w in q.split()) if t]
    if not tokens:
        return qs
    rows = list(qs.values_list("pk", "title", "filename", "channel", "tags__name"))
    by_pk = {}
    for pk, title, filename, channel, tag in rows:
        entry = by_pk.setdefault(pk, [title, filename, channel or "", []])
        if tag:
            entry[3].append(tag)

    hits = [pk for pk, (title, filename, channel, tags) in by_pk.items()
            if all(tok in _squash(" ".join((title or "", filename or "", channel, *tags)))
                   for tok in tokens)]
    if hits:
        return qs.filter(pk__in=hits)

    # Fuzzy fallback for typos (e.g. "vidoe" -> "video"). Compare PER WORD, not
    # the whole title (a short query barely moves the ratio against a long
    # title): a video matches if every query word closely matches *some* word
    # in its name. Very short words and first-letter mismatches are skipped,
    # otherwise junk like "zzzqq" "matches" a title containing "zzz".
    query_words = [w.casefold() for w in _WORD_RE.findall(q)] or tokens
    hits = []
    for pk, (title, filename, channel, tags) in by_pk.items():
        name_words = _WORD_RE.findall(" ".join((title or "", filename or "", channel, *tags)).casefold())
        if not name_words:
            continue
        if all(
            len(qw) >= _FUZZY_MIN_LEN
            and max((difflib.SequenceMatcher(None, qw, nw).ratio()
                     for nw in name_words if nw[:1] == qw[:1]), default=0) >= _FUZZY_THRESHOLD
            for qw in query_words
        ):
            hits.append(pk)
    return qs.filter(pk__in=hits) if hits else qs.none()


def _filtered_videos(request):
    qs = Video.objects.filter(missing=False, hidden=False)
    q = request.GET.get("q", "").strip()
    if q:
        qs = _search_filter(qs, q)
    tag = request.GET.get("tag", "").strip()
    if tag:
        qs = qs.filter(tags__name=tag)
    if request.GET.get("fav") == "1":
        qs = qs.filter(favorite=True)
    if request.GET.get("shorts") == "1":
        qs = qs.filter(PORTRAIT_Q)
    channel = request.GET.get("channel", "").strip()
    if channel:
        qs = qs.filter(channel=channel)
    sort = request.GET.get("sort", "added")
    rev  = request.GET.get("rev") == "1"
    if sort == "rating":
        qs = qs.order_by(*(RATING_ORDER_REV if rev else RATING_ORDER))
    else:
        order = SORTS.get(sort, "-date_added")
        if rev:
            order = order.lstrip("-") if order.startswith("-") else f"-{order}"
        qs = qs.order_by(order)
    return qs, q, sort, rev


# ---- pages -----------------------------------------------------------------

@public_when_enabled
def home(request):
    u = _state_user(request)
    recent = Video.objects.filter(missing=False, hidden=False).order_by("-date_added")[:12]
    continue_watching = (
        Video.objects.filter(missing=False, hidden=False,
                             playback_states__user=u, playback_states__finished=False,
                             playback_states__position_seconds__gt=5)
        # shorts and < 3 min videos never resume (Video.remembers_position)
        .exclude(PORTRAIT_Q).exclude(duration_seconds__lt=RESUME_MIN_SECONDS)
        .order_by("-playback_states__updated_at")[:12]
    )

    recent_watch_ids = list(
        WatchEvent.objects.filter(user=u, video__missing=False)
        .order_by("-watched_at").values_list("video_id", flat=True)[:5]
    )
    tag_ids = list(
        Video.objects.filter(pk__in=recent_watch_ids)
        .values_list("tags", flat=True).distinct()
    )
    tag_ids = [t for t in tag_ids if t]
    discovery = []
    if tag_ids:
        discovery = list(
            Video.objects.filter(tags__in=tag_ids, missing=False, hidden=False)
            .exclude(pk__in=recent_watch_ids)
            .exclude(playback_states__user=u, playback_states__finished=True)
            .distinct().order_by("?")[:12]
        )
    if len(discovery) < 6 and recent_watch_ids:
        excl = set(recent_watch_ids) | {v.pk for v in discovery}
        discovery += list(
            Video.objects.filter(missing=False, hidden=False).exclude(pk__in=excl)
            .exclude(playback_states__user=u, playback_states__finished=True).order_by("-date_added")
            [:12 - len(discovery)]
        )

    return render(request, "library/home.html", base_ctx(
        request, active_nav="home", page_id="home", spa_title="HomeFlix",
        recent=_attach_playback(recent, u),
        continue_watching=_attach_playback(continue_watching, u),
        discovery=_attach_playback(discovery, u),
        total=Video.objects.filter(missing=False, hidden=False).count(),
    ))


@public_when_enabled
def library(request):
    qs, q, sort, rev = _filtered_videos(request)
    total_secs = Video.objects.filter(missing=False, hidden=False).aggregate(s=Sum('duration_seconds'))['s'] or 0
    h, rem = divmod(int(total_secs), 3600)
    m = rem // 60
    total_duration = f"{h}h {m}m" if h else (f"{m}m" if m else "")
    return render(request, "library/library.html", base_ctx(
        request, active_nav="library", page_id="library", spa_title="Library — HomeFlix",
        q=q, sort=sort, rev=rev,
        page_size=settings.PAGE_SIZE,
        tags=Tag.objects.all(), fav=request.GET.get("fav") == "1",
        active_tag=request.GET.get("tag", ""),
        channel=request.GET.get("channel", "").strip(),
        total_videos=Video.objects.filter(missing=False, hidden=False).count(),
        total_duration=total_duration,
    ))


CHANNEL_SORTS = ("count", "name", "recent")


@public_when_enabled
def channels(request):
    """One card per channel (author): a collage of its newest videos'
    thumbnails, the name, and how many videos it has. Clicking opens the
    Library filtered to that channel. Videos with no channel are only
    counted (the owner can fill authors in from the Downloads page)."""
    sort = request.GET.get("sort", "count")
    if sort not in CHANNEL_SORTS:
        sort = "count"
    base = Video.objects.filter(missing=False, hidden=False)
    named = base.exclude(channel="")
    rows = list(named.values("channel").annotate(n=Count("id"), latest=Max("date_added")))
    if sort == "name":
        rows.sort(key=lambda r: r["channel"].casefold())
    elif sort == "recent":
        rows.sort(key=lambda r: r["latest"], reverse=True)
    else:
        rows.sort(key=lambda r: (-r["n"], r["channel"].casefold()))
    covers = {}
    for name, vid in (named.exclude(thumbnail_path="").order_by("-date_added")
                      .values_list("channel", "id").iterator()):
        if len(covers.setdefault(name, [])) < 4:
            covers[name].append(vid)
    items = [{"name": r["channel"], "count": r["n"], "covers": covers.get(r["channel"], [])}
             for r in rows]
    return render(request, "library/channels.html", base_ctx(
        request, active_nav="channels", page_id="channels", spa_title="Channels — HomeFlix",
        channels=items, sort=sort, no_channel=base.filter(channel="").count()))


@owner_required
def hidden_videos(request):
    videos = Video.objects.filter(missing=False, hidden=True).order_by("-date_added")
    total_hidden = videos.count()
    return render(request, "library/hidden.html", base_ctx(
        request, active_nav="", page_id="hidden", spa_title="Hidden — HomeFlix",
        videos=_attach_playback(videos, request.user), total_hidden=total_hidden,
    ))


def history(request):
    events = list(WatchEvent.objects.select_related("video")
                  .filter(user=request.user, video__missing=False)[:200])
    _attach_playback([e.video for e in events], request.user)
    return render(request, "library/history.html", base_ctx(
        request, active_nav="history", spa_title="History — HomeFlix",
        events=events))


@public_when_enabled
def playlists(request):
    return render(request, "library/playlists.html", base_ctx(
        request, active_nav="playlists", page_id="playlists", spa_title="Playlists — HomeFlix",
        playlists=Playlist.objects.all(),
        smart_playlists=SmartPlaylist.objects.all(),
        total_hidden=Video.objects.filter(missing=False, hidden=True).count()))


def _remembered_sort(request, key, allowed, default):
    """(sort, rev) for a playlist page, remembered per account and playlist.
    An explicit ?sort= wins and is saved; with none, the saved choice (or
    `default`) is used. Anonymous visitors (HOMEFLIX_PUBLIC) just get the
    default -- UserPref ignores them. Unknown sort names are never saved."""
    if "sort" in request.GET:
        sort, rev = request.GET.get("sort", ""), request.GET.get("rev") == "1"
        if sort in allowed:
            value = f"{sort}:{int(rev)}"
            if UserPref.get(request.user, key, "") != value:
                UserPref.set(request.user, key, value)
        return sort, rev
    sort, _, r = UserPref.get(request.user, key, "").partition(":")
    return (sort, r == "1") if sort in allowed else (default, False)


@public_when_enabled
def playlist_detail(request, pk):
    pl = get_object_or_404(Playlist, pk=pk)
    sort, rev = _remembered_sort(request, f"plsort_{pk}", {*PLAYLIST_SORTS, "rating"}, "manual")
    if sort == "rating":
        order = PLAYLIST_RATING_ORDER_REV if rev else PLAYLIST_RATING_ORDER
    else:
        order = (_apply_sort(PLAYLIST_SORTS.get(sort, "order"), rev),)
    items = list(pl.items.select_related("video").filter(video__missing=False)
                 .order_by(*order))
    _attach_playback([it.video for it in items], _state_user(request))
    return render(request, "library/playlist_detail.html", base_ctx(
        request, active_nav="playlists", page_id="playlist_detail",
        spa_title=f"{pl.name} — HomeFlix",
        playlist=pl, items=items, sort=sort, rev=rev))


# ---- watch (single-page player) --------------------------------------------

def _serialize_subs(pk, video):
    """Subtitle list in the shape the player's CC popover expects. Manual
    (uploaded) tracks additionally get a remove_url for the × button."""
    out = []
    for i, t in enumerate(services.list_subtitles(video)):
        row = {"idx": i, "label": t["label"], "lang": t["lang"], "url": f"/subs/{pk}/{i}.vtt"}
        if t["kind"] == "manual":
            row["remove_url"] = f"/video/{pk}/subtitles/{t['id']}/delete/"
        out.append(row)
    return out


def _build_watch_data(request, pk):
    """Serialize everything the player overlay needs, for SSR and the JSON API."""
    from .templatetags.library_extras import duration as fmt_dur, filesize as fmt_size
    import random as _rnd

    u = _state_user(request)
    video = get_object_or_404(Video, pk=pk, missing=False)
    if video.hidden and not request.user.is_staff:
        raise Http404("No such video")

    # "Next"/"prev" queue: which list of videos this play was opened from.
    # Cards carry that context in the watch URL's query string (pl=, sp=,
    # ctx=history/hidden) so ▶ next follows the page you actually opened it
    # from instead of always falling back to the default library order.
    pl_id = request.GET.get("pl")
    sp_id = request.GET.get("sp")
    ctx = request.GET.get("ctx")
    queue_ids = []
    if pl_id:
        queue_ids = list(
            PlaylistItem.objects.filter(playlist_id=pl_id)
            .order_by("order").values_list("video_id", flat=True)
        )
    elif sp_id:
        sp = SmartPlaylist.objects.filter(pk=sp_id).first()
        if sp:
            queue_ids = list(sp.get_videos().values_list("pk", flat=True))
    elif ctx == "history":
        # WatchEvent is already deduped for consecutive repeats (see below) —
        # collapse here too in case older rows predate that fix.
        seen = []
        for vid in (WatchEvent.objects.filter(user=u, video__missing=False)
                    .order_by("-watched_at").values_list("video_id", flat=True)):
            if not seen or seen[-1] != vid:
                seen.append(vid)
        queue_ids = seen
    elif ctx == "hidden":
        queue_ids = list(
            Video.objects.filter(missing=False, hidden=True).values_list("id", flat=True)
        )
    if video.id not in queue_ids:
        qs, _q, _s, _r = _filtered_videos(request)
        queue_ids = list(qs.values_list("id", flat=True))

    prev_id = next_id = None
    if video.id in queue_ids:
        i = queue_ids.index(video.id)
        if i > 0:
            prev_id = queue_ids[i - 1]
        if i < len(queue_ids) - 1:
            next_id = queue_ids[i + 1]

    # Anonymous (HOMEFLIX_PUBLIC) visitor: no account to attach a resume row
    # or history entry to. Skip both rather than writing a shared "anonymous"
    # row -- that would make every anonymous visitor's progress/history bleed
    # into every other anonymous visitor's.
    if u is not None:
        state, _ = PlaybackState.objects.get_or_create(video=video, user=u)
        # Only log a new History row when it's a different video than the most
        # recent one — _build_watch_data runs on every player open *and* every
        # re-fetch (favorite toggle, add-to-playlist, download, etc.), so without
        # this the same video watched/reopened repeatedly fills History with runs
        # of duplicates.
        last_event = WatchEvent.objects.filter(user=u).order_by("-id").first()
        if not last_event or last_event.video_id != video.id:
            WatchEvent.objects.create(video=video, user=u, progress_seconds=state.position_seconds)
    else:
        state = None

    tag_ids = list(video.tags.values_list("pk", flat=True))
    recommended = []
    if tag_ids:
        recommended += list(
            Video.objects.filter(tags__in=tag_ids, missing=False, hidden=False)
            .exclude(pk=video.pk).exclude(playback_states__user=u, playback_states__finished=True)
            .distinct().order_by("?")[:8]
        )
    if video.channel and len(recommended) < 8:
        excl = {video.pk} | {v.pk for v in recommended}
        recommended += list(
            Video.objects.filter(channel=video.channel, missing=False, hidden=False)
            .exclude(pk__in=excl).exclude(playback_states__user=u, playback_states__finished=True)
            .order_by("?")[:4]
        )
    if len(recommended) < 12:
        excl = {video.pk} | {v.pk for v in recommended}
        recommended += list(
            Video.objects.filter(missing=False, hidden=False).exclude(pk__in=excl)
            .order_by("?")[:12 - len(recommended)]
        )
    _rnd.shuffle(recommended)
    recommended = recommended[:12]

    rec_pks = [v.pk for v in recommended]
    rec_states = {ps.video_id: ps
                  for ps in PlaybackState.objects.filter(video_id__in=rec_pks, user=u)}

    def rec_dict(v):
        st = rec_states.get(v.pk)
        progress = 0
        if st and v.duration_seconds and v.remembers_position:
            progress = min(100, st.position_seconds / v.duration_seconds * 100)
        return {
            "id": v.pk, "title": v.title,
            "thumb_url": f"/thumb/{v.pk}/?v={_thumb_v(v.thumbnail_path)}" if v.thumbnail_path else "",
            "watch_url": f"/watch/{v.pk}/",
            "dur": fmt_dur(v.duration_seconds), "channel": v.channel or "",
            "progress": round(progress, 1), "quality": v.aspect_label or "",
            "needs_convert": v.needs_convert_ui,
        }

    tech = []
    if video.aspect_label:
        tech.append(f"{video.aspect_label} · {video.width}×{video.height}")
    if video.duration_seconds:
        tech.append(fmt_dur(video.duration_seconds))
    if video.video_codec:
        c = video.video_codec + (f" / {video.audio_codec}" if video.audio_codec else "")
        tech.append(c)
    if video.size_bytes:
        tech.append(fmt_size(video.size_bytes))

    if settings.REMOTE_ROOT:
        remote_path = settings.REMOTE_ROOT.rstrip("\\/") + "\\" + video.rel_path.replace("/", "\\")
    else:
        remote_path = video.file_path

    # Non-browser-playable files (mkv, HEVC, exotic audio, ...) play via a live
    # HLS transcode (Jellyfin-style) — unless a converted MP4 copy already
    # exists, in which case just play that natively (it's a clean file).
    converted_ready = (video.convert_status == Video.CONVERT_DONE
                       and video.converted_path and os.path.exists(video.converted_path))
    use_hls = (not video.browser_playable) and not converted_ready

    # Resume position resets once you've watched most of it, so reopening a
    # video you finished (or nearly did) starts fresh instead of a few
    # seconds from the end. The stored position (and card progress bar) is
    # untouched — only the resume *start point* for this open is affected.
    resume_pos = state.position_seconds if state else 0.0
    if video.duration_seconds and resume_pos >= 0.8 * video.duration_seconds:
        resume_pos = 0.0
    if not video.remembers_position:   # shorts / < 3 min: always from the start
        resume_pos = 0.0

    subs = _serialize_subs(pk, video)

    return {
        "id": video.pk, "title": video.title,
        "description": video.description or "", "channel": video.channel or "",
        "stream_url": f"/stream/{pk}/", "watch_url": f"/watch/{pk}/",
        "download_url": f"/stream/{pk}/?download=1",
        "save_url": f"/video/{pk}/progress/",
        "position": resume_pos,
        "remember_position": video.remembers_position,
        "duration_label": fmt_dur(video.duration_seconds),
        "playable": video.playable_now,
        "hls": use_hls,
        "hls_url": f"/hls/{pk}/index.m3u8",
        "hls_stop_url": f"/hls/{pk}/stop/",
        "subtitles": subs,
        "duration_seconds": video.duration_seconds or 0,
        "convert_status": video.convert_status,
        "convert_progress": video.convert_progress,
        "convert_url": f"/video/{pk}/convert/",
        "convert_status_url": f"/video/{pk}/convert/status/",
        "convert_cancel_url": f"/video/{pk}/convert/cancel/",
        "needs_convert": video.needs_convert_ui,
        "repeat": UserPref.get(u, "repeat", "off"),
        "repeat_toggle_url": "/repeat/",
        "shuffle": UserPref.get(u, "shuffle", "0") == "1",
        "shuffle_toggle_url": "/shuffle/",
        # Server value wins for a logged-in user (follows the account across
        # devices) -- but ONLY once one has been saved. None means "this
        # account never set one", so the client keeps (and uploads) its own
        # localStorage value instead of a server default clobbering it.
        "seek_step": _saved_seek_step(u),
        "seek_step_url": "/seek-step/",
        "next_id": next_id, "prev_id": prev_id,
        "queue_ids": queue_ids,
        "thumb_url": f"/thumb/{pk}/?v={_thumb_v(video.thumbnail_path)}" if video.thumbnail_path else "",
        "regen_thumb_url": f"/video/{pk}/thumb/regen/",
        "thumbnail_percent": video.thumbnail_percent or 0,
        "rename_url": f"/video/{pk}/rename/",
        "filename_stem": os.path.splitext(video.filename)[0],
        "filename_ext": os.path.splitext(video.filename)[1],
        "source_url": video.source_url or "", "tech": tech,
        "favorite": video.favorite, "rating": video.rating,
        "favorite_url": f"/video/{pk}/favorite/",
        "rating_url": f"/video/{pk}/rating/",
        "playlist_url": f"/video/{pk}/playlist/",
        "autoplay_toggle_url": "/autoplay/",
        "autoplay": UserPref.get(u, "autoplay", "1") == "1",
        "notes": [
            {"id": n.pk, "ts": n.timestamp_seconds,
             "ts_label": fmt_dur(n.timestamp_seconds), "text": n.text,
             "delete_url": f"/video/{pk}/notes/{n.pk}/delete/"}
            for n in video.notes.all()
        ],
        "note_add_url": f"/video/{pk}/notes/",
        "recommended": [rec_dict(v) for v in recommended],
        "playlists": [{"id": p.pk, "name": p.name} for p in Playlist.objects.all()],
        "ext": (video.ext or "").upper(),
        "is_portrait": video.is_portrait,
        "aspect": round(video.width / video.height, 4) if (video.width and video.height) else None,
        "frame_url_base": f"/frame/{pk}/",
        "pl": pl_id or "",
        "remote_path": remote_path,
        "hidden": video.hidden,
        "hide_url": f"/video/{pk}/hide/",
        "delete_url": f"/video/{pk}/delete/",
        "is_owner": request.user.is_authenticated and request.user.is_staff,
    }


@public_when_enabled
def watch(request, pk):
    """Hard load of /watch/<id>/ -> render the shell and auto-open the player."""
    data = _build_watch_data(request, pk)
    return render(request, "library/watch.html", base_ctx(
        request, active_nav="", page_id="watch",
        spa_title=f"{data['title']} — HomeFlix",
        auto_watch=data,
    ))


@public_when_enabled
def watch_api(request, pk):
    """JSON for the player — used by card clicks and prev/next/recommendations."""
    return JsonResponse(_build_watch_data(request, pk))


# ---- media serving ---------------------------------------------------------

def _thumb_file_response(request, path, content_type="image/jpeg"):
    """Serve a thumbnail file with long-lived Cache-Control + Last-Modified so
    TV browsers stop re-downloading every tile on each scroll / card-recycle
    (the grid is virtualized -- see makeCard in base.html -- which destroys
    and recreates <img> nodes as you scroll). 304s on repeat requests instead
    of re-transferring the whole JPEG."""
    mtime = os.path.getmtime(path)
    if not was_modified_since(request.META.get("HTTP_IF_MODIFIED_SINCE"), mtime):
        return HttpResponseNotModified()
    resp = FileResponse(open(path, "rb"), content_type=content_type)
    resp["Cache-Control"] = "public, max-age=31536000"
    resp["Last-Modified"] = http_date(mtime)
    return resp


@public_when_enabled
def thumb(request, pk):
    video = get_object_or_404(Video, pk=pk)
    if video.thumbnail_path and os.path.exists(video.thumbnail_path):
        return _thumb_file_response(request, video.thumbnail_path)
    raise Http404("No thumbnail")


@public_when_enabled
def playlist_thumb(request, pk):
    pl = get_object_or_404(Playlist, pk=pk)
    if pl.thumbnail_path and os.path.exists(pl.thumbnail_path):
        content_type = mimetypes.guess_type(pl.thumbnail_path)[0] or "image/jpeg"
        return _thumb_file_response(request, pl.thumbnail_path, content_type)
    raise Http404("No thumbnail")


@public_when_enabled
def frame_thumb(request, pk, t):
    video = get_object_or_404(Video, pk=pk)
    dur = int(video.duration_seconds or 0)
    t = max(0, min(dur, (int(t) // 5) * 5))
    frame_dir = os.path.join(settings.THUMBNAIL_DIR, "frames")
    os.makedirs(frame_dir, exist_ok=True)
    path = os.path.join(frame_dir, f"{video.id}_{t}.jpg")
    if not os.path.exists(path):
        src = (video.converted_path
               if video.convert_status == Video.CONVERT_DONE and video.converted_path
               else video.file_path)
        code, _, _ = services._run([
            "ffmpeg", "-y", "-ss", str(t), "-i", src,
            "-frames:v", "1", "-vf", "scale=240:-1", "-q:v", "6", path,
        ])
        if code != 0 or not os.path.exists(path):
            raise Http404("Frame unavailable")
    return FileResponse(open(path, "rb"), content_type="image/jpeg")


@public_when_enabled
def stream(request, pk):
    video = get_object_or_404(Video, pk=pk)
    if video.hidden and not request.user.is_staff:
        raise Http404("No such video")
    path = video.file_path
    if (video.convert_status == Video.CONVERT_DONE and video.converted_path
            and os.path.exists(video.converted_path)):
        path = video.converted_path
    if not os.path.exists(path):
        raise Http404("File missing")

    download = request.GET.get("download") == "1"

    size = os.path.getsize(path)
    content_type = (mimetypes.guess_type(path)[0]
                     or EXTRA_MIME_TYPES.get(os.path.splitext(path)[1].lower())
                     or "application/octet-stream")
    range_header = "" if download else request.META.get("HTTP_RANGE", "")
    match = RANGE_RE.match(range_header)

    if match:
        start = int(match.group(1))
        if start >= size:
            resp = HttpResponse(status=416)
            resp["Content-Range"] = f"bytes */{size}"
            return resp
        end = int(match.group(2)) if match.group(2) else size - 1
        end = min(end, size - 1)
        if not match.group(2):
            # Open-ended "bytes=N-": answer with a bounded chunk, not the whole
            # rest of the file. The browser asks for the next range when it
            # needs it. Streaming the entire remainder in one response keeps a
            # gunicorn worker busy for as long as the player takes to consume
            # it (minutes), and gunicorn SIGKILLs a sync worker after --timeout
            # seconds -- which cut playback off mid-video with no recovery.
            end = min(end, start + MAX_RANGE_BYTES - 1)
        length = end - start + 1

        def chunks():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    data = f.read(min(CHUNK, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        resp = StreamingHttpResponse(chunks(), status=206, content_type=content_type)
        resp["Content-Range"] = f"bytes {start}-{end}/{size}"
        resp["Content-Length"] = str(length)
    else:
        resp = FileResponse(open(path, "rb"), content_type=content_type)
        resp["Content-Length"] = str(size)

    resp["Accept-Ranges"] = "bytes"
    if download:
        resp["Content-Disposition"] = f'attachment; filename="{os.path.basename(path)}"'
    return resp


# ---- actions (POST) --------------------------------------------------------

@require_POST
@owner_required
def scan(request):
    services.scan_library()
    return redirect(request.META.get("HTTP_REFERER", "/"))


@require_POST
@owner_required
def regen_thumb(request, pk):
    video = get_object_or_404(Video, pk=pk)
    try:
        percent = float(request.POST.get("percent", 0))
    except ValueError:
        percent = 0.0
    percent = max(0.0, min(100.0, percent))
    services.generate_thumbnail(video, percent=percent)
    if is_spa(request):
        return JsonResponse({"ok": True})
    return redirect(request.META.get("HTTP_REFERER", "/"))


@require_POST
@owner_required
def rename_video_view(request, pk):
    video = get_object_or_404(Video, pk=pk)
    title = request.POST.get("title")
    stem = request.POST.get("filename")
    ok, err = services.rename_video(video, new_title=title, new_stem=stem)
    if not ok:
        return JsonResponse({"ok": False, "error": err}, status=400)
    return JsonResponse({
        "ok": True, "title": video.title,
        "filename_stem": os.path.splitext(video.filename)[0],
        "filename_ext": os.path.splitext(video.filename)[1],
    })


_PLAYLIST_THUMB_EXTS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".png": "image/png", ".webp": "image/webp"}


def _clear_thumb_file(obj):
    if obj.thumbnail_path and os.path.exists(obj.thumbnail_path):
        try:
            os.remove(obj.thumbnail_path)
        except OSError:
            pass


def _upload_thumb(request, obj, out_prefix):
    f = request.FILES.get("image")
    if not f:
        return JsonResponse({"ok": False, "error": "No file"})
    ext = os.path.splitext(f.name)[1].lower()
    if ext not in _PLAYLIST_THUMB_EXTS:
        return JsonResponse({"ok": False, "error": "Unsupported image type"})
    _clear_thumb_file(obj)
    os.makedirs(settings.THUMBNAIL_DIR, exist_ok=True)
    out_path = os.path.join(settings.THUMBNAIL_DIR, f"{out_prefix}{ext}")
    with open(out_path, "wb") as out:
        for chunk in f.chunks():
            out.write(chunk)
    obj.thumbnail_path = out_path
    obj.save(update_fields=["thumbnail_path"])
    return JsonResponse({"ok": True})


@require_POST
def playlist_thumb_generate(request, pk):
    pl = get_object_or_404(Playlist, pk=pk)
    _clear_thumb_file(pl)
    # Not filtering by video__missing here: a video already added to the
    # playlist should still count toward its cover even if flagged missing
    # (it already has a cached thumbnail from before it went missing).
    videos = [it.video for it in pl.items.select_related("video")[:4]]
    out_path = os.path.join(settings.THUMBNAIL_DIR, f"playlist_{pk}.jpg")
    path, error = services.generate_collage_thumbnail(pl, videos, out_path)
    return JsonResponse({"ok": bool(path), "error": error})


@require_POST
def playlist_thumb_remove(request, pk):
    pl = get_object_or_404(Playlist, pk=pk)
    _clear_thumb_file(pl)
    pl.thumbnail_path = ""
    pl.save(update_fields=["thumbnail_path"])
    return JsonResponse({"ok": True})


@require_POST
def playlist_thumb_upload(request, pk):
    pl = get_object_or_404(Playlist, pk=pk)
    return _upload_thumb(request, pl, f"playlist_{pk}")


@public_when_enabled
def smart_playlist_thumb(request, pk):
    sp = get_object_or_404(SmartPlaylist, pk=pk)
    if sp.thumbnail_path and os.path.exists(sp.thumbnail_path):
        content_type = mimetypes.guess_type(sp.thumbnail_path)[0] or "image/jpeg"
        return _thumb_file_response(request, sp.thumbnail_path, content_type)
    raise Http404("No thumbnail")


@require_POST
def smart_playlist_thumb_generate(request, pk):
    sp = get_object_or_404(SmartPlaylist, pk=pk)
    _clear_thumb_file(sp)
    videos = list(sp.get_videos()[:4])
    out_path = os.path.join(settings.THUMBNAIL_DIR, f"smart_playlist_{pk}.jpg")
    path, error = services.generate_collage_thumbnail(sp, videos, out_path)
    return JsonResponse({"ok": bool(path), "error": error})


@require_POST
def smart_playlist_thumb_remove(request, pk):
    sp = get_object_or_404(SmartPlaylist, pk=pk)
    _clear_thumb_file(sp)
    sp.thumbnail_path = ""
    sp.save(update_fields=["thumbnail_path"])
    return JsonResponse({"ok": True})


@require_POST
def smart_playlist_thumb_upload(request, pk):
    sp = get_object_or_404(SmartPlaylist, pk=pk)
    return _upload_thumb(request, sp, f"smart_playlist_{pk}")


@require_POST
@public_when_enabled
def save_progress(request, pk):
    u = _state_user(request)
    if u is None:
        # Anonymous (HOMEFLIX_PUBLIC) visitor: no account to persist to.
        # Silent no-op -- the video keeps playing, it just won't resume next
        # time, which is correct for a visitor with no account.
        return JsonResponse({"ok": True})
    video = get_object_or_404(Video, pk=pk)
    if not video.remembers_position:
        return JsonResponse({"ok": True})   # shorts / < 3 min: no resume point is kept
    try:
        pos = float(request.POST.get("position", 0))
    except ValueError:
        pos = 0.0
    state, _ = PlaybackState.objects.get_or_create(video=video, user=u)
    state.position_seconds = pos
    if video.duration_seconds and pos >= 0.9 * video.duration_seconds:
        state.finished = True
    elif pos < 5:
        state.finished = False
    state.save()
    return JsonResponse({"ok": True})


@require_POST
def toggle_favorite(request, pk):
    video = get_object_or_404(Video, pk=pk)
    video.favorite = not video.favorite
    video.save(update_fields=["favorite"])
    return JsonResponse({"favorite": video.favorite})


@require_POST
@owner_required
def toggle_hidden(request, pk):
    video = get_object_or_404(Video, pk=pk)
    video.hidden = not video.hidden
    video.save(update_fields=["hidden"])
    return JsonResponse({"hidden": video.hidden})


@require_POST
@owner_required
def delete_video(request, pk):
    video = get_object_or_404(Video, pk=pk)
    _delete_videos([video], delete_file=request.POST.get("mode") == "file")
    return JsonResponse({"ok": True})


@require_POST
def set_rating(request, pk):
    video = get_object_or_404(Video, pk=pk)
    try:
        rating = int(request.POST.get("rating", 0))
    except ValueError:
        rating = 0
    video.rating = max(0, min(5, rating))
    video.save(update_fields=["rating"])
    return JsonResponse({"rating": video.rating})


@require_POST
@public_when_enabled
def toggle_theme(request):
    new = "light" if theme(request) == "dark" else "dark"
    UserPref.set(request.user, "theme", new)
    return JsonResponse({"theme": new})


@require_POST
@public_when_enabled
def toggle_autoplay(request):
    new = "0" if UserPref.get(request.user, "autoplay", "1") == "1" else "1"
    UserPref.set(request.user, "autoplay", new)
    return JsonResponse({"autoplay": new == "1"})


@require_POST
@owner_required
def set_default_thumb_percent(request):
    try:
        pct = float(request.POST.get("percent", 0))
    except ValueError:
        pct = 0.0
    pct = max(0.0, min(100.0, pct))
    Setting.set("default_thumb_percent", pct)
    return JsonResponse({"default_thumb_percent": pct})


@require_POST
@public_when_enabled
def toggle_repeat(request):
    # Cycle: off -> all (loop the whole queue) -> one (loop this video) -> off.
    order = ["off", "all", "one"]
    cur = UserPref.get(request.user, "repeat", "off")
    new = order[(order.index(cur) + 1) % len(order)] if cur in order else "off"
    UserPref.set(request.user, "repeat", new)
    return JsonResponse({"repeat": new})


@require_POST
@public_when_enabled
def toggle_shuffle(request):
    new = "0" if UserPref.get(request.user, "shuffle", "0") == "1" else "1"
    UserPref.set(request.user, "shuffle", new)
    return JsonResponse({"shuffle": new == "1"})


@require_POST
@public_when_enabled
def set_seek_step(request):
    """The ◀▶/media-key/double-tap seek amount, synced per-account so it
    follows you across devices (like autoplay/repeat/shuffle). Anonymous
    (HOMEFLIX_PUBLIC) visitors keep working here too -- UserPref.set() is a
    silent no-op for them, so the client just falls back to its own
    localStorage-only value (see setSeekStep() in base.html)."""
    try:
        v = max(1, min(600, int(float(request.POST.get("value", 10)))))
    except ValueError:
        v = 10
    UserPref.set(request.user, "seek_step", v)
    return JsonResponse({"seek_step": v})


@require_POST
def create_playlist(request):
    name = request.POST.get("name", "").strip()
    if name:
        Playlist.objects.get_or_create(name=name)
    return redirect("playlists")


@require_POST
def add_to_playlist(request, pk):
    video = get_object_or_404(Video, pk=pk)
    pl_pk = request.POST.get("playlist")
    new_name = request.POST.get("new_name", "").strip()
    if not pl_pk and new_name:
        pl, _ = Playlist.objects.get_or_create(name=new_name)
    elif pl_pk:
        pl = get_object_or_404(Playlist, pk=pl_pk)
    else:
        if is_spa(request):
            return JsonResponse({"ok": False})
        return redirect(request.META.get("HTTP_REFERER", "/"))
    order = pl.items.count()
    PlaylistItem.objects.get_or_create(playlist=pl, video=video,
                                       defaults={"order": order})
    if is_spa(request):
        return JsonResponse({"ok": True, "playlist": pl.name, "playlist_id": pl.pk})
    return redirect(request.META.get("HTTP_REFERER", "/"))


def _fmt_date(d):
    """Locale-independent 'Jul 5, 2026' -- strftime's %-d (no leading zero) is
    a glibc-only extension; the Windows C runtime raises ValueError on it,
    which took down the whole /api/videos/ grid on Windows."""
    return f"{d:%b} {d.day}, {d:%Y}"


def _thumb_v(path):
    """Cache-busting token for thumbnail URLs: the mtime of the file on disk.
    /thumb/<pk>/ is served with a year-long Cache-Control (see
    _thumb_file_response) so TV browsers stop re-downloading every tile; a
    plain regen wouldn't otherwise be visible until the cache expired since
    the URL never changed. Appending ?v=<mtime> busts the cache exactly when
    (and only when) the file actually changes."""
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return 0


# ---- Infinite-scroll JSON API ----------------------------------------------
def _serialize(video, qs_suffix=""):
    state = next(iter(getattr(video, "my_playback", ())), None)
    progress = 0
    if state and video.duration_seconds and video.remembers_position:
        progress = min(100, (state.position_seconds / video.duration_seconds) * 100)
    from .templatetags.library_extras import duration as fmt_dur
    return {
        "id": video.id, "title": video.title,
        "dur": fmt_dur(video.duration_seconds),
        "thumb": f"/thumb/{video.id}/?v={_thumb_v(video.thumbnail_path)}" if video.thumbnail_path else "",
        "url": f"/watch/{video.id}/{qs_suffix}",
        "quality": video.aspect_label,
        "needs_convert": video.needs_convert_ui,
        "channel": video.channel,
        "date": _fmt_date(video.date_added),
        "favorite": video.favorite,
        "progress": round(progress, 1),
    }


@public_when_enabled
def api_videos(request):
    qs, _q, _sort, _rev = _filtered_videos(request)
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except ValueError:
        page = 1
    size = settings.PAGE_SIZE
    start = (page - 1) * size
    total = qs.count()
    filt = request.GET.copy()
    filt.pop("page", None)
    qs_suffix = f"?{filt.urlencode()}" if filt else ""
    page_videos = _attach_playback(qs[start:start + size], _state_user(request))
    items = [_serialize(v, qs_suffix) for v in page_videos]
    return JsonResponse({
        "items": items, "page": page, "total": total,
        "has_more": start + size < total,
    })


# ---- Conversion endpoints ---------------------------------------------------
@require_POST
@owner_required
def convert(request, pk):
    video = get_object_or_404(Video, pk=pk)
    services.start_conversion(video)
    return JsonResponse({"status": video.convert_status})


def convert_status(request, pk):
    video = get_object_or_404(Video, pk=pk)
    return JsonResponse({
        "status": video.convert_status,
        "progress": video.convert_progress,
        "ready": video.convert_status == Video.CONVERT_DONE,
    })


@require_POST
@owner_required
def cancel_convert(request, pk):
    video = get_object_or_404(Video, pk=pk)
    services.cancel_conversion(video)
    return JsonResponse({"ok": True})


# ---- Live HLS playback ------------------------------------------------------
import time as _time

_HLS_SEG_RE = re.compile(r"^seg_\d+\.ts$")
_HLS_JS_PATH = os.path.join(os.path.dirname(__file__), "vendor", "hls.min.js")


@public_when_enabled
def hls_playlist(request, pk):
    """Start (or reuse) the live transcode and serve its .m3u8. hls.js re-fetches
    this periodically to pick up newly-produced segments."""
    video = get_object_or_404(Video, pk=pk)
    if video.hidden and not request.user.is_staff:
        raise Http404("No such video")
    if not os.path.exists(video.file_path):
        raise Http404("File missing")
    session_dir = services.start_hls(video)
    m3u8 = os.path.join(session_dir, "index.m3u8")
    # Wait for ffmpeg to emit the playlist + first segment (usually ~1-2s).
    for _ in range(50):  # up to ~10s
        if os.path.exists(m3u8) and os.path.getsize(m3u8) > 0:
            break
        _time.sleep(0.2)
    if not (os.path.exists(m3u8) and os.path.getsize(m3u8) > 0):
        return HttpResponse("HLS warming up", status=503)
    resp = FileResponse(open(m3u8, "rb"), content_type="application/vnd.apple.mpegurl")
    resp["Cache-Control"] = "no-store"
    return resp


@public_when_enabled
def hls_segment(request, pk, name):
    if not _HLS_SEG_RE.match(name):
        raise Http404("Bad segment")
    path = os.path.join(services._hls_dir(pk), name)
    if not os.path.exists(path):
        raise Http404("Segment not ready")
    services.touch_hls(pk)
    resp = FileResponse(open(path, "rb"), content_type="video/mp2t")
    resp["Cache-Control"] = "no-store"
    return resp


@require_POST
def hls_stop(request, pk):
    services.stop_hls(pk)
    return JsonResponse({"ok": True})


@public_when_enabled
def hls_js(request):
    if not os.path.exists(_HLS_JS_PATH):
        raise Http404("hls.js not vendored")
    resp = FileResponse(open(_HLS_JS_PATH, "rb"), content_type="application/javascript")
    resp["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ---- Subtitles (sidecar / embedded -> cached WebVTT) ------------------------
@public_when_enabled
def subtitles(request, pk, idx):
    video = get_object_or_404(Video, pk=pk)
    if not os.path.exists(video.file_path):
        raise Http404("File missing")
    path = services.ensure_subtitle_vtt(video, idx)
    if not path:
        raise Http404("Subtitle not available")
    resp = FileResponse(open(path, "rb"), content_type="text/vtt")
    resp["Cache-Control"] = "no-store"
    return resp


@require_POST
def upload_subtitle(request, pk):
    """Attach a subtitle file that isn't (or won't stay) next to the video --
    e.g. one sitting in an unrelated folder, or about to be cleaned up. Stored
    as an app-owned WebVTT (services.store_uploaded_subtitle), decoupled from
    the original file's location."""
    video = get_object_or_404(Video, pk=pk)
    upload = request.FILES.get("file")
    if not upload:
        return JsonResponse({"ok": False, "error": "No file"}, status=400)
    sub, err_code, detail = services.store_uploaded_subtitle(
        video, upload, request.POST.get("label", ""), request.POST.get("lang", ""))
    if not sub:
        msg = {"bad_extension": "Unsupported file type", "too_large": "File too large",
               "conversion_failed": "Couldn't read that as a subtitle file"}.get(err_code, "Upload failed")
        if detail:
            msg += f": {detail}"
        return JsonResponse({"ok": False, "error": msg, "error_code": err_code}, status=400)
    return JsonResponse({"ok": True, "subtitles": _serialize_subs(pk, video)})


@require_POST
def delete_subtitle(request, pk, sub_pk):
    video = get_object_or_404(Video, pk=pk)
    sub = get_object_or_404(VideoSubtitle, pk=sub_pk, video=video)
    services.delete_uploaded_subtitle(sub)
    return JsonResponse({"ok": True, "subtitles": _serialize_subs(pk, video)})


# ---- Organize / maintenance -------------------------------------------------
@owner_required
def organize(request):
    if request.method == "POST" and request.POST.get("confirm") == "1":
        result = services.organize_by_mtime(execute=True)
        return render(request, "library/organize.html", base_ctx(
            request, page_id="organize", spa_title="Organize — HomeFlix",
            result=result, done=True))
    result = services.organize_by_mtime(execute=False)
    return render(request, "library/organize.html", base_ctx(
        request, page_id="organize", spa_title="Organize — HomeFlix",
        result=result, done=False))


@owner_required
def duplicates(request):
    groups = services.find_duplicates()
    _attach_playback([v for g in groups for v in g], request.user)
    return render(request, "library/duplicates.html", base_ctx(
        request, page_id="duplicates", spa_title="Duplicates — HomeFlix",
        groups=groups,
    ))


@require_POST
@owner_required
def purge_missing(request):
    services.purge_missing()
    return redirect(request.META.get("HTTP_REFERER", "/"))


@require_POST
@owner_required
def reset_library(request):
    services.reset_library()
    services.scan_library()
    return redirect("home")


# ---- Random video ----------------------------------------------------------
@public_when_enabled
def shorts(request):
    videos = (Video.objects.filter(missing=False, hidden=False).filter(PORTRAIT_Q)
              .order_by("-date_added"))
    return render(request, "library/shorts.html", base_ctx(
        request, active_nav="shorts", page_id="shorts", spa_title="Shorts — HomeFlix",
        videos=videos,
    ))


@public_when_enabled
def random_video(request):
    import random as _rnd
    pl_id = request.GET.get("pl")
    sp_id = request.GET.get("sp")
    has_filter = any(request.GET.get(k) for k in ("q", "tag", "fav", "shorts", "channel"))
    if pl_id:
        ids = list(PlaylistItem.objects.filter(playlist_id=pl_id).values_list("video_id", flat=True))
        if not ids:
            return redirect("playlist_detail", pk=pl_id)
        pk = _rnd.choice(ids)
    elif sp_id:
        sp = get_object_or_404(SmartPlaylist, pk=sp_id)
        ids = list(sp.get_videos().values_list("pk", flat=True))
        if not ids:
            return redirect("smart_playlist_detail", pk=sp_id)
        pk = _rnd.choice(ids)
    elif has_filter:
        qs, _q, _s, _r = _filtered_videos(request)
        ids = list(qs.values_list("pk", flat=True))
        if not ids:
            return redirect("library")
        pk = _rnd.choice(ids)
    else:
        count = Video.objects.filter(missing=False, hidden=False).count()
        if not count:
            return redirect("library")
        pk = Video.objects.filter(missing=False, hidden=False)[_rnd.randrange(count)].pk
    filt = request.GET.copy()
    filt.pop("json", None)
    watch_url = f"/watch/{pk}/" + (f"?{filt.urlencode()}" if filt else "")
    if is_spa(request) or request.GET.get("json") == "1":
        return JsonResponse({"id": pk, "watch_url": watch_url})
    return redirect(watch_url)


# ---- Notes -----------------------------------------------------------------
@require_POST
def add_note(request, pk):
    from .models import VideoNote
    video = get_object_or_404(Video, pk=pk)
    text = request.POST.get("text", "").strip()
    try:
        ts = max(0.0, float(request.POST.get("timestamp", 0)))
    except ValueError:
        ts = 0.0
    if not text:
        return JsonResponse({"ok": False})
    from .templatetags.library_extras import duration as fmt_dur
    note = VideoNote.objects.create(video=video, text=text, timestamp_seconds=ts)
    return JsonResponse({"ok": True, "id": note.pk, "ts": ts,
                         "ts_label": fmt_dur(ts), "text": text})


@require_POST
def delete_note(request, pk, note_pk):
    from .models import VideoNote
    note = get_object_or_404(VideoNote, pk=note_pk, video_id=pk)
    note.delete()
    return JsonResponse({"ok": True})


# ---- Playlist delete -------------------------------------------------------
@require_POST
def delete_playlist(request, pk):
    pl = get_object_or_404(Playlist, pk=pk)
    pl.delete()
    return redirect("playlists")


# ---- Playlist reorder -------------------------------------------------------
@require_POST
def reorder_playlist(request, pk):
    """Persist a new manual order for a playlist's items. `ids` is a
    comma-separated list of video ids in the desired order (as dragged in
    the UI) -- items are re-numbered 0..n so "Playlist order" sort matches."""
    pl = get_object_or_404(Playlist, pk=pk)
    ids_param = request.POST.get("ids", "")
    try:
        ids = [int(x) for x in ids_param.split(",") if x.strip()]
    except ValueError:
        return JsonResponse({"ok": False, "error": "bad ids"}, status=400)
    items = {it.video_id: it for it in pl.items.filter(video_id__in=ids)}
    updated = []
    for i, vid in enumerate(ids):
        item = items.get(vid)
        if item and item.order != i:
            item.order = i
            updated.append(item)
    if updated:
        PlaylistItem.objects.bulk_update(updated, ["order"])
    return JsonResponse({"ok": True})


# ---- Smart playlists -------------------------------------------------------
from .models import SmartPlaylist


@public_when_enabled
def smart_playlist_detail(request, pk):
    sp = get_object_or_404(SmartPlaylist, pk=pk)
    sort, rev = _remembered_sort(request, f"spsort_{pk}", {*SORTS, "rating"}, "added")
    if sort == "rating":
        order = RATING_ORDER_REV if rev else RATING_ORDER
    else:
        order = (_apply_sort(SORTS.get(sort, "-date_added"), rev),)
    videos = list(sp.get_videos().order_by(*order)[:200])
    _attach_playback(videos, _state_user(request))
    return render(request, "library/smart_playlist_detail.html", base_ctx(
        request, active_nav="playlists", page_id="smart_playlist",
        spa_title=f"{sp.name} — HomeFlix", sp=sp, videos=videos, sort=sort, rev=rev))


@require_POST
def create_smart_playlist(request):
    name = request.POST.get("name", "").strip()
    if name:
        sp = SmartPlaylist.objects.create(name=name)
        return redirect("smart_playlist_detail", pk=sp.pk)
    return redirect("playlists")


@require_POST
def delete_smart_playlist(request, pk):
    sp = get_object_or_404(SmartPlaylist, pk=pk)
    sp.delete()
    return redirect("playlists")


@require_POST
def save_smart_rules(request, pk):
    sp = get_object_or_404(SmartPlaylist, pk=pk)
    try:
        rules = json.loads(request.POST.get("rules", "[]"))
        json.dumps(rules)
    except Exception:
        rules = []
    sp.rules = json.dumps(rules)
    sp.name  = request.POST.get("name", sp.name).strip() or sp.name
    match = request.POST.get("match", sp.match_mode)
    sp.match_mode = match if match in (SmartPlaylist.MATCH_ALL, SmartPlaylist.MATCH_ANY) else sp.match_mode
    sp.save()
    return redirect("smart_playlist_detail", pk=sp.pk)


# ---- Bulk actions ----------------------------------------------------------

@require_POST
@owner_required
def bulk_regen_thumb(request):
    try:
        ids = [int(i) for i in request.POST.get('ids', '').split(',') if i.strip()]
        percent = max(0.0, min(100.0, float(request.POST.get('percent', 0))))
    except ValueError:
        return JsonResponse({'ok': False})
    for video in Video.objects.filter(pk__in=ids):
        services.generate_thumbnail(video, percent=percent)
    return JsonResponse({'ok': True, 'count': len(ids)})


@require_POST
def bulk_rating(request):
    try:
        ids = [int(i) for i in request.POST.get('ids', '').split(',') if i.strip()]
        rating = max(0, min(5, int(request.POST.get('rating', 0))))
    except ValueError:
        return JsonResponse({'ok': False})
    Video.objects.filter(pk__in=ids).update(rating=rating)
    return JsonResponse({'ok': True, 'count': len(ids)})


@require_POST
def bulk_add_playlist(request):
    try:
        ids = [int(i) for i in request.POST.get('ids', '').split(',') if i.strip()]
    except ValueError:
        return JsonResponse({'ok': False})
    new_name = request.POST.get('new_name', '').strip()
    if new_name:
        pl, _ = Playlist.objects.get_or_create(name=new_name)
    else:
        pl = get_object_or_404(Playlist, pk=request.POST.get('playlist'))
    order = pl.items.count()
    added = 0
    for vid_id in ids:
        try:
            video = Video.objects.get(pk=vid_id)
            _, created = PlaylistItem.objects.get_or_create(
                playlist=pl, video=video, defaults={'order': order + added})
            if created:
                added += 1
        except Video.DoesNotExist:
            pass
    return JsonResponse({'ok': True, 'count': added, 'playlist_id': pl.pk, 'playlist': pl.name})


@require_POST
def bulk_favorite(request):
    try:
        ids = [int(i) for i in request.POST.get('ids', '').split(',') if i.strip()]
    except ValueError:
        return JsonResponse({'ok': False})
    Video.objects.filter(pk__in=ids).update(favorite=True)
    return JsonResponse({'ok': True, 'count': len(ids)})


@require_POST
@owner_required
def bulk_rename(request):
    ids = [int(i) for i in request.POST.get('ids', '').split(',') if i.strip()]
    pattern = request.POST.get('pattern', '')
    if not ids or not pattern.strip():
        return JsonResponse({'ok': False})
    try:
        start = int(request.POST.get('start', 1))
        pad = max(1, min(6, int(request.POST.get('pad', 2))))
    except ValueError:
        return JsonResponse({'ok': False})
    count = services.bulk_rename_titles(ids, pattern, start=start, pad=pad)
    return JsonResponse({'ok': True, 'count': count})


@require_POST
@owner_required
def bulk_hide(request):
    try:
        ids = [int(i) for i in request.POST.get('ids', '').split(',') if i.strip()]
    except ValueError:
        return JsonResponse({'ok': False})
    Video.objects.filter(pk__in=ids).update(hidden=True)
    return JsonResponse({'ok': True, 'count': len(ids)})


@require_POST
@owner_required
def bulk_unhide(request):
    try:
        ids = [int(i) for i in request.POST.get('ids', '').split(',') if i.strip()]
    except ValueError:
        return JsonResponse({'ok': False})
    Video.objects.filter(pk__in=ids).update(hidden=False)
    return JsonResponse({'ok': True, 'count': len(ids)})


def _delete_videos(videos, delete_file):
    """Shared by the single-video and bulk delete endpoints. Always removes
    the DB row + thumbnail; only touches the real file when delete_file=True
    (the default is DB-only, for files that are just unwanted from the
    library but shouldn't be destroyed on disk)."""
    count = 0
    for video in videos:
        if delete_file:
            for p in (video.file_path, video.converted_path):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        if video.thumbnail_path and os.path.exists(video.thumbnail_path):
            try:
                os.remove(video.thumbnail_path)
            except OSError:
                pass
        services.cleanup_video_subtitles(video)
        video.delete()
        count += 1
    return count


@require_POST
@owner_required
def bulk_delete(request):
    try:
        ids = [int(i) for i in request.POST.get('ids', '').split(',') if i.strip()]
    except ValueError:
        return JsonResponse({'ok': False})
    count = _delete_videos(Video.objects.filter(pk__in=ids), request.POST.get('mode') == 'file')
    return JsonResponse({'ok': True, 'count': count})


@public_when_enabled
def api_playlists(request):
    return JsonResponse({'playlists': [{'id': p.pk, 'name': p.name}
                                       for p in Playlist.objects.all()]})


# ---- PWA manifest + icon ---------------------------------------------------

@login_not_required
def pwa_manifest(request):
    return JsonResponse({
        "name": "HomeFlix",
        "short_name": "HomeFlix",
        "description": "Your personal local video library",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0e0f12",
        "theme_color": "#f0654a",
        "icons": [
            {"src": "/icons/192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icons/512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }, content_type="application/manifest+json")


@login_not_required
def pwa_icon(request, size):
    import struct, zlib
    if size not in (16, 32, 48, 64, 96, 128, 180, 192, 256, 512):
        raise Http404
    br, bg, bb = 240, 101, 74   # #f0654a background
    fr, fg, fb = 255, 255, 255  # white play triangle
    x1, y1 = int(size * 0.30), int(size * 0.20)
    x2, y2 = int(size * 0.30), int(size * 0.80)
    x3, y3 = int(size * 0.75), int(size * 0.50)
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            d1 = (x - x2) * (y1 - y2) - (x1 - x2) * (y - y2)
            d2 = (x - x3) * (y2 - y3) - (x2 - x3) * (y - y3)
            d3 = (x - x1) * (y3 - y1) - (x3 - x1) * (y - y1)
            if not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0)):
                row += bytes([fr, fg, fb])
            else:
                row += bytes([br, bg, bb])
        rows.append(b'\x00' + bytes(row))
    raw = b''.join(rows)

    def _chunk(tag, data):
        c = tag + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

    png = (b'\x89PNG\r\n\x1a\n' +
           _chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)) +
           _chunk(b'IDAT', zlib.compress(raw, 9)) +
           _chunk(b'IEND', b''))
    resp = HttpResponse(png, content_type='image/png')
    resp['Cache-Control'] = 'public, max-age=86400'
    return resp


# ---- Playlist downloader (owner only) ---------------------------------------
# Everything here writes to the library folder or stores YouTube credentials,
# so every endpoint -- reads included -- is @owner_required. JSON endpoints are
# fetched with X-SPA by the page (see getJSON() in base.html) so a non-owner
# gets the JSON 403 rather than the HTML "not allowed" page.

def _dl_source_json(src):
    return {"id": src.pk, "url": src.url, "name": src.name or src.url,
            "subdir": src.target_subdir,
            "fetched": src.last_fetched.isoformat() if src.last_fetched else ""}


def _dl_job_json(job):
    if not job:
        return None
    return {"id": job.pk, "source": job.source_id, "status": job.status,
            "pct": job.current_pct, "title": job.current_title, "total": job.total,
            "done": job.done_count, "fail": job.fail_count, "summary": job.summary,
            "log": job.log_tail,
            "running": job.status in (DownloadJob.QUEUED, DownloadJob.RUNNING)}


def _dl_tools_json():
    rt = downloader.js_runtime()
    return {"installed": downloader.ytdlp_available(), "version": downloader.ytdlp_version(),
            "runtime": {"name": rt["name"], "version": rt["version"], "supported": rt["supported"],
                        "problem": downloader.runtime_problem(rt)},
            "cookies": downloader.cookie_status(),
            "update": downloader.update_status()}


def _dl_state_json(src):
    entries = downloader.source_state(src)
    counts = {}
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    return {"ok": True, "source": _dl_source_json(src), "entries": entries, "counts": counts,
            "disk": downloader.disk_info(src)}


def _dl_ids(request, src):
    """Posted ids that are real video ids belonging to this source's last fetch
    -- never arbitrary client-supplied strings."""
    known = {e["id"] for e in json.loads(src.entries_json or "[]")}
    return [i for i in dict.fromkeys(request.POST.getlist("ids"))
            if downloader.VIDEO_ID_RE.match(i) and i in known]


@owner_required
def downloads(request):
    downloader.reap_stale()
    return render(request, "library/downloads.html", base_ctx(
        request, page_id="downloads", spa_title="Download — HomeFlix",
        sources=[_dl_source_json(s) for s in DownloadSource.objects.all()],
        tools=_dl_tools_json(),
        refresh=refresh.status(),
        job=_dl_job_json(DownloadJob.objects.select_related("source").first()),
    ))


@owner_required
def dl_tools(request):
    return JsonResponse({"ok": True, **_dl_tools_json()})


@require_POST
@owner_required
def dl_cookies(request):
    if request.POST.get("action") == "clear":
        downloader.clear_cookies()
        return JsonResponse({"ok": True, "cookies": downloader.cookie_status()})
    try:
        downloader.save_cookies(request.POST.get("text", ""))
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return JsonResponse({"ok": True, "cookies": downloader.cookie_status()})


@require_POST
@owner_required
def dl_refresh(request):
    """Update already-downloaded videos' thumbnail/author/title from YouTube."""
    fields = {k: request.POST.get(k) == "1" for k in ("thumbs", "channel", "title")}
    if not any(fields.values()):
        return JsonResponse({"ok": False, "error": "Pick at least one thing to update"}, status=400)
    if not downloader.ytdlp_available():
        return JsonResponse({"ok": False, "error": "yt-dlp isn't installed on the server"}, status=400)
    if not refresh.start_refresh(fields):
        return JsonResponse({"ok": False, "error": "An update is already running"}, status=409)
    return JsonResponse({"ok": True, "refresh": refresh.status()})


@owner_required
def dl_refresh_status(request):
    return JsonResponse({"ok": True, "refresh": refresh.status()})


@require_POST
@owner_required
def dl_update_ytdlp(request):
    return JsonResponse({"ok": True, "started": downloader.start_update()})


@require_POST
@owner_required
def dl_source_add(request):
    try:
        url = downloader.normalize_url(request.POST.get("url", ""))
        subdir = request.POST.get("subdir", "").strip()
        downloader.resolve_target(subdir)
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    src, _created = DownloadSource.objects.get_or_create(
        url=url, defaults={"target_subdir": subdir})
    if src.target_subdir != subdir:
        src.target_subdir = subdir
        src.save(update_fields=["target_subdir"])
    return JsonResponse({"ok": True, "source": _dl_source_json(src)})


@require_POST
@owner_required
def dl_source_delete(request, pk):
    get_object_or_404(DownloadSource, pk=pk).delete()
    return JsonResponse({"ok": True})


@require_POST
@owner_required
def dl_source_fetch(request, pk):
    src = get_object_or_404(DownloadSource, pk=pk)
    name, entries, err = downloader.fetch_playlist(src.url)
    if err:
        return JsonResponse({"ok": False, "error": err}, status=502)
    src.name = name or src.name
    src.entries_json = json.dumps(entries)
    src.last_fetched = timezone.now()
    src.save(update_fields=["name", "entries_json", "last_fetched"])
    return JsonResponse(_dl_state_json(src))


@owner_required
def dl_source_state(request, pk):
    return JsonResponse(_dl_state_json(get_object_or_404(DownloadSource, pk=pk)))


@require_POST
@owner_required
def dl_source_skip(request, pk):
    src = get_object_or_404(DownloadSource, pk=pk)
    titles = {e["id"]: e.get("title", "") for e in json.loads(src.entries_json or "[]")}
    for vid in _dl_ids(request, src):
        SkippedEntry.objects.get_or_create(source=src, video_id=vid,
                                           defaults={"title": titles.get(vid, "")[:500]})
    return JsonResponse(_dl_state_json(src))


@require_POST
@owner_required
def dl_source_unskip(request, pk):
    src = get_object_or_404(DownloadSource, pk=pk)
    SkippedEntry.objects.filter(source=src, video_id__in=_dl_ids(request, src)).delete()
    return JsonResponse(_dl_state_json(src))


@require_POST
@owner_required
def dl_source_start(request, pk):
    src = get_object_or_404(DownloadSource, pk=pk)
    ids = _dl_ids(request, src)
    if not ids:
        return JsonResponse({"ok": False, "error": "Nothing selected"}, status=400)
    job, err = downloader.start_job(src, ids)
    if err:
        return JsonResponse({"ok": False, "error": err}, status=409)
    return JsonResponse({"ok": True, "job": _dl_job_json(job)})


@owner_required
def dl_job_status(request):
    downloader.reap_stale()
    job = DownloadJob.objects.select_related("source").first()
    return JsonResponse({"ok": True, "job": _dl_job_json(job)})


@require_POST
@owner_required
def dl_job_cancel(request):
    return JsonResponse({"ok": downloader.cancel_job()})


# ---- Backups (owner only) ---------------------------------------------------

@owner_required
def backups(request):
    return render(request, "library/backups.html", base_ctx(
        request, page_id="backups", spa_title="Backups — HomeFlix", status=backup.status()))


@owner_required
def backups_status(request):
    return JsonResponse({"ok": True, **backup.status()})


@require_POST
@owner_required
def backups_save(request):
    try:
        backup.save_config(request.POST.get("enabled") == "1", request.POST.get("hours"),
                           request.POST.get("path"), request.POST.get("keep"))
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return JsonResponse({"ok": True, **backup.status()})


@require_POST
@owner_required
def backups_run(request):
    if not backup.start_now():
        return JsonResponse({"ok": False, "error": "A backup is already running"}, status=409)
    return JsonResponse({"ok": True, **backup.status()})
