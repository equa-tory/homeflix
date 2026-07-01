import os
import re
import json
import mimetypes

from django.conf import settings
from django.db.models import Q, Sum
from django.http import (
    StreamingHttpResponse, HttpResponse, JsonResponse, Http404, FileResponse,
)
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.http import require_POST

from .models import Video, PlaybackState, WatchEvent, Playlist, PlaylistItem, Tag, Setting
from . import services

RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")
CHUNK = 8192


# ---- helpers ---------------------------------------------------------------

def theme(request):
    return Setting.get("theme", "dark")


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
        # The page templates extend this. Full load -> shell; SPA fetch -> fragment.
        "base_template": "library/_spa.html" if is_spa(request) else "library/base.html",
    }
    ctx.update(extra)
    return ctx


SORTS = {
    "added": "-date_added",
    "name": "title",
    "duration": "-duration_seconds",
    "modified": "-file_mtime",
}


def _filtered_videos(request):
    qs = Video.objects.filter(missing=False)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(filename__icontains=q))
    tag = request.GET.get("tag", "").strip()
    if tag:
        qs = qs.filter(tags__name=tag)
    if request.GET.get("fav") == "1":
        qs = qs.filter(favorite=True)
    if request.GET.get("shorts") == "1":
        from django.db.models import F as _F
        qs = qs.filter(height__gt=_F("width"), width__gt=0)
    sort = request.GET.get("sort", "added")
    rev  = request.GET.get("rev") == "1"
    order = SORTS.get(sort, "-date_added")
    if rev:
        order = order.lstrip("-") if order.startswith("-") else f"-{order}"
    qs = qs.order_by(order)
    return qs, q, sort, rev


# ---- pages -----------------------------------------------------------------

def home(request):
    recent = Video.objects.filter(missing=False).order_by("-date_added")[:12]
    continue_watching = (
        Video.objects.filter(missing=False, playback__finished=False,
                             playback__position_seconds__gt=5)
        .select_related("playback").order_by("-playback__updated_at")[:12]
    )

    recent_watch_ids = list(
        WatchEvent.objects.filter(video__missing=False)
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
            Video.objects.filter(tags__in=tag_ids, missing=False)
            .exclude(pk__in=recent_watch_ids)
            .exclude(playback__finished=True)
            .distinct().order_by("?")[:12]
        )
    if len(discovery) < 6 and recent_watch_ids:
        excl = set(recent_watch_ids) | {v.pk for v in discovery}
        discovery += list(
            Video.objects.filter(missing=False).exclude(pk__in=excl)
            .exclude(playback__finished=True).order_by("-date_added")
            [:12 - len(discovery)]
        )

    return render(request, "library/home.html", base_ctx(
        request, active_nav="home", page_id="home", spa_title="HomeFlix",
        recent=recent, continue_watching=continue_watching,
        discovery=discovery,
        total=Video.objects.filter(missing=False).count(),
    ))


def library(request):
    qs, q, sort, rev = _filtered_videos(request)
    total_secs = Video.objects.filter(missing=False).aggregate(s=Sum('duration_seconds'))['s'] or 0
    h, rem = divmod(int(total_secs), 3600)
    m = rem // 60
    total_duration = f"{h}h {m}m" if h else (f"{m}m" if m else "")
    return render(request, "library/library.html", base_ctx(
        request, active_nav="library", page_id="library", spa_title="Library — HomeFlix",
        q=q, sort=sort, rev=rev,
        page_size=settings.PAGE_SIZE,
        tags=Tag.objects.all(), fav=request.GET.get("fav") == "1",
        active_tag=request.GET.get("tag", ""),
        total_videos=Video.objects.filter(missing=False).count(),
        total_duration=total_duration,
    ))


def history(request):
    events = (WatchEvent.objects.select_related("video")
              .filter(video__missing=False)[:200])
    return render(request, "library/history.html", base_ctx(
        request, active_nav="history", spa_title="History — HomeFlix",
        events=events))


def playlists(request):
    return render(request, "library/playlists.html", base_ctx(
        request, active_nav="playlists", page_id="playlists", spa_title="Playlists — HomeFlix",
        playlists=Playlist.objects.all(),
        smart_playlists=SmartPlaylist.objects.all()))


def playlist_detail(request, pk):
    pl = get_object_or_404(Playlist, pk=pk)
    items = pl.items.select_related("video").filter(video__missing=False)
    return render(request, "library/playlist_detail.html", base_ctx(
        request, active_nav="playlists", spa_title=f"{pl.name} — HomeFlix",
        playlist=pl, items=items))


# ---- watch (single-page player) --------------------------------------------

def _build_watch_data(request, pk):
    """Serialize everything the player overlay needs, for SSR and the JSON API."""
    from .templatetags.library_extras import duration as fmt_dur, filesize as fmt_size
    import random as _rnd

    video = get_object_or_404(Video, pk=pk, missing=False)

    pl_id = request.GET.get("pl")
    queue_ids = []
    if pl_id:
        queue_ids = list(
            PlaylistItem.objects.filter(playlist_id=pl_id)
            .order_by("order").values_list("video_id", flat=True)
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

    state, _ = PlaybackState.objects.get_or_create(video=video)
    WatchEvent.objects.create(video=video, progress_seconds=state.position_seconds)

    tag_ids = list(video.tags.values_list("pk", flat=True))
    recommended = []
    if tag_ids:
        recommended += list(
            Video.objects.filter(tags__in=tag_ids, missing=False)
            .exclude(pk=video.pk).exclude(playback__finished=True)
            .distinct().order_by("?")[:8]
        )
    if video.channel and len(recommended) < 8:
        excl = {video.pk} | {v.pk for v in recommended}
        recommended += list(
            Video.objects.filter(channel=video.channel, missing=False)
            .exclude(pk__in=excl).exclude(playback__finished=True)
            .order_by("?")[:4]
        )
    if len(recommended) < 12:
        excl = {video.pk} | {v.pk for v in recommended}
        recommended += list(
            Video.objects.filter(missing=False).exclude(pk__in=excl)
            .order_by("?")[:12 - len(recommended)]
        )
    _rnd.shuffle(recommended)
    recommended = recommended[:12]

    rec_pks = [v.pk for v in recommended]
    rec_states = {ps.video_id: ps
                  for ps in PlaybackState.objects.filter(video_id__in=rec_pks)}

    def rec_dict(v):
        st = rec_states.get(v.pk)
        progress = 0
        if st and v.duration_seconds:
            progress = min(100, st.position_seconds / v.duration_seconds * 100)
        return {
            "id": v.pk, "title": v.title,
            "thumb_url": f"/thumb/{v.pk}/" if v.thumbnail_path else "",
            "watch_url": f"/watch/{v.pk}/",
            "dur": fmt_dur(v.duration_seconds), "channel": v.channel or "",
            "progress": round(progress, 1), "quality": v.aspect_label or "",
            "needs_convert": not v.browser_playable and v.convert_status != Video.CONVERT_DONE,
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

    return {
        "id": video.pk, "title": video.title,
        "description": video.description or "", "channel": video.channel or "",
        "stream_url": f"/stream/{pk}/", "watch_url": f"/watch/{pk}/",
        "save_url": f"/video/{pk}/progress/",
        "position": state.position_seconds,
        "duration_label": fmt_dur(video.duration_seconds),
        "playable": video.playable_now,
        "convert_status": video.convert_status,
        "convert_progress": video.convert_progress,
        "convert_url": f"/video/{pk}/convert/",
        "convert_status_url": f"/video/{pk}/convert/status/",
        "needs_convert": video.needs_conversion,
        "next_id": next_id, "prev_id": prev_id,
        "thumb_url": f"/thumb/{pk}/" if video.thumbnail_path else "",
        "regen_thumb_url": f"/video/{pk}/thumb/regen/",
        "thumbnail_percent": video.thumbnail_percent or 0,
        "source_url": video.source_url or "", "tech": tech,
        "favorite": video.favorite, "rating": video.rating,
        "favorite_url": f"/video/{pk}/favorite/",
        "rating_url": f"/video/{pk}/rating/",
        "playlist_url": f"/video/{pk}/playlist/",
        "autoplay_toggle_url": "/autoplay/",
        "autoplay": Setting.get("autoplay", "1") == "1",
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
        "is_portrait": bool(video.height and video.width and video.height > video.width),
        "frame_url_base": f"/frame/{pk}/",
        "pl": pl_id or "",
    }


def watch(request, pk):
    """Hard load of /watch/<id>/ -> render the shell and auto-open the player."""
    data = _build_watch_data(request, pk)
    return render(request, "library/watch.html", base_ctx(
        request, active_nav="", page_id="watch",
        spa_title=f"{data['title']} — HomeFlix",
        auto_watch=data,
    ))


def watch_api(request, pk):
    """JSON for the player — used by card clicks and prev/next/recommendations."""
    return JsonResponse(_build_watch_data(request, pk))


# ---- media serving ---------------------------------------------------------

def thumb(request, pk):
    video = get_object_or_404(Video, pk=pk)
    if video.thumbnail_path and os.path.exists(video.thumbnail_path):
        return FileResponse(open(video.thumbnail_path, "rb"),
                            content_type="image/jpeg")
    raise Http404("No thumbnail")


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


def stream(request, pk):
    video = get_object_or_404(Video, pk=pk)
    path = video.file_path
    if (video.convert_status == Video.CONVERT_DONE and video.converted_path
            and os.path.exists(video.converted_path)):
        path = video.converted_path
    if not os.path.exists(path):
        raise Http404("File missing")

    size = os.path.getsize(path)
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    range_header = request.META.get("HTTP_RANGE", "")
    match = RANGE_RE.match(range_header)

    if match:
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
        end = min(end, size - 1)
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
    return resp


# ---- actions (POST) --------------------------------------------------------

@require_POST
def scan(request):
    services.scan_library()
    return redirect(request.META.get("HTTP_REFERER", "/"))


@require_POST
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
def save_progress(request, pk):
    video = get_object_or_404(Video, pk=pk)
    try:
        pos = float(request.POST.get("position", 0))
    except ValueError:
        pos = 0.0
    state, _ = PlaybackState.objects.get_or_create(video=video)
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
def toggle_theme(request):
    new = "light" if theme(request) == "dark" else "dark"
    Setting.set("theme", new)
    return JsonResponse({"theme": new})


@require_POST
def toggle_autoplay(request):
    new = "0" if Setting.get("autoplay", "1") == "1" else "1"
    Setting.set("autoplay", new)
    return JsonResponse({"autoplay": new == "1"})


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


# ---- Infinite-scroll JSON API ----------------------------------------------
def _serialize(video):
    state = getattr(video, "playback", None)
    progress = 0
    if state and video.duration_seconds:
        progress = min(100, (state.position_seconds / video.duration_seconds) * 100)
    from .templatetags.library_extras import duration as fmt_dur
    return {
        "id": video.id, "title": video.title,
        "dur": fmt_dur(video.duration_seconds),
        "thumb": f"/thumb/{video.id}/" if video.thumbnail_path else "",
        "url": f"/watch/{video.id}/",
        "quality": video.aspect_label,
        "needs_convert": not video.browser_playable and video.convert_status != Video.CONVERT_DONE,
        "channel": video.channel,
        "date": video.date_added.strftime("%b %-d, %Y"),
        "favorite": video.favorite,
        "progress": round(progress, 1),
    }


def api_videos(request):
    qs, _q, _sort, _rev = _filtered_videos(request)
    qs = qs.select_related("playback")
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except ValueError:
        page = 1
    size = settings.PAGE_SIZE
    start = (page - 1) * size
    total = qs.count()
    items = [_serialize(v) for v in qs[start:start + size]]
    return JsonResponse({
        "items": items, "page": page, "total": total,
        "has_more": start + size < total,
    })


# ---- Conversion endpoints ---------------------------------------------------
@require_POST
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


# ---- Organize / maintenance -------------------------------------------------
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


@require_POST
def purge_missing(request):
    services.purge_missing()
    return redirect(request.META.get("HTTP_REFERER", "/"))


@require_POST
def reset_library(request):
    services.reset_library()
    services.scan_library()
    return redirect("home")


# ---- Random video ----------------------------------------------------------
def shorts(request):
    from django.db.models import F
    videos = (Video.objects.filter(missing=False, height__gt=F("width"), width__gt=0)
              .order_by("-date_added"))
    return render(request, "library/shorts.html", base_ctx(
        request, active_nav="shorts", page_id="shorts", spa_title="Shorts — HomeFlix",
        videos=videos,
    ))


def random_video(request):
    import random as _rnd
    count = Video.objects.filter(missing=False).count()
    if not count:
        return redirect("library")
    video = Video.objects.filter(missing=False)[_rnd.randrange(count)]
    if is_spa(request) or request.GET.get("json") == "1":
        return JsonResponse({"id": video.pk, "watch_url": f"/watch/{video.pk}/"})
    return redirect("watch", pk=video.pk)


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


# ---- Smart playlists -------------------------------------------------------
from .models import SmartPlaylist


def smart_playlist_detail(request, pk):
    sp = get_object_or_404(SmartPlaylist, pk=pk)
    videos = list(sp.get_videos()[:200])
    return render(request, "library/smart_playlist_detail.html", base_ctx(
        request, active_nav="playlists", page_id="smart_playlist",
        spa_title=f"{sp.name} — HomeFlix", sp=sp, videos=videos))


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
    sp.save()
    return redirect("smart_playlist_detail", pk=sp.pk)


# ---- Bulk actions ----------------------------------------------------------

@require_POST
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
    return JsonResponse({'ok': True, 'count': added})


@require_POST
def bulk_favorite(request):
    try:
        ids = [int(i) for i in request.POST.get('ids', '').split(',') if i.strip()]
    except ValueError:
        return JsonResponse({'ok': False})
    Video.objects.filter(pk__in=ids).update(favorite=True)
    return JsonResponse({'ok': True, 'count': len(ids)})


def api_playlists(request):
    return JsonResponse({'playlists': [{'id': p.pk, 'name': p.name}
                                       for p in Playlist.objects.all()]})


# ---- PWA manifest + icon ---------------------------------------------------

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
