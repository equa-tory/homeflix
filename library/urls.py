from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("library/", views.library, name="library"),
    path("hidden/", views.hidden_videos, name="hidden_videos"),
    path("history/", views.history, name="history"),
    path("playlists/", views.playlists, name="playlists"),
    path("playlists/<int:pk>/", views.playlist_detail, name="playlist_detail"),
    path("playlists/<int:pk>/thumbnail/", views.playlist_thumb, name="playlist_thumb"),
    path("playlists/<int:pk>/thumbnail/generate/", views.playlist_thumb_generate, name="playlist_thumb_generate"),
    path("playlists/<int:pk>/thumbnail/remove/", views.playlist_thumb_remove, name="playlist_thumb_remove"),
    path("playlists/<int:pk>/thumbnail/upload/", views.playlist_thumb_upload, name="playlist_thumb_upload"),
    path("watch/<int:pk>/", views.watch, name="watch"),

    # media
    path("thumb/<int:pk>/", views.thumb, name="thumb"),
    path("stream/<int:pk>/", views.stream, name="stream"),
    path("frame/<int:pk>/<int:t>/", views.frame_thumb, name="frame_thumb"),

    # live HLS playback (Jellyfin-style on-the-fly transcode)
    path("hls.js", views.hls_js, name="hls_js"),
    path("hls/<int:pk>/index.m3u8", views.hls_playlist, name="hls_playlist"),
    path("hls/<int:pk>/stop/", views.hls_stop, name="hls_stop"),
    path("hls/<int:pk>/<str:name>", views.hls_segment, name="hls_segment"),
    path("subs/<int:pk>/<int:idx>.vtt", views.subtitles, name="subtitles"),

    # api
    path("api/videos/", views.api_videos, name="api_videos"),
    path("api/watch/<int:pk>/", views.watch_api, name="watch_api"),

    # conversion
    path("video/<int:pk>/convert/", views.convert, name="convert"),
    path("video/<int:pk>/convert/status/", views.convert_status, name="convert_status"),
    path("video/<int:pk>/convert/cancel/", views.cancel_convert, name="cancel_convert"),

    # organize / maintenance
    path("organize/", views.organize, name="organize"),
    path("purge-missing/", views.purge_missing, name="purge_missing"),
    path("reset-library/", views.reset_library, name="reset_library"),

    # actions
    path("scan/", views.scan, name="scan"),
    path("video/<int:pk>/thumb/regen/", views.regen_thumb, name="regen_thumb"),
    path("video/<int:pk>/progress/", views.save_progress, name="save_progress"),
    path("video/<int:pk>/favorite/", views.toggle_favorite, name="toggle_favorite"),
    path("video/<int:pk>/hide/", views.toggle_hidden, name="toggle_hidden"),
    path("video/<int:pk>/delete/", views.delete_video, name="delete_video"),
    path("video/<int:pk>/rating/", views.set_rating, name="set_rating"),
    path("video/<int:pk>/playlist/", views.add_to_playlist, name="add_to_playlist"),
    path("theme/", views.toggle_theme, name="toggle_theme"),
    path("shorts/", views.shorts, name="shorts"),
    path("random/", views.random_video, name="random_video"),
    path("video/<int:pk>/notes/", views.add_note, name="add_note"),
    path("video/<int:pk>/notes/<int:note_pk>/delete/", views.delete_note, name="delete_note"),
    path("autoplay/", views.toggle_autoplay, name="toggle_autoplay"),
    path("loop/", views.toggle_loop, name="toggle_loop"),
    path("playlists/create/", views.create_playlist, name="create_playlist"),
    path("playlists/<int:pk>/delete/", views.delete_playlist, name="delete_playlist"),
    path("smart-playlists/create/", views.create_smart_playlist, name="create_smart_playlist"),
    path("smart-playlists/<int:pk>/", views.smart_playlist_detail, name="smart_playlist_detail"),
    path("smart-playlists/<int:pk>/rules/", views.save_smart_rules, name="save_smart_rules"),
    path("smart-playlists/<int:pk>/delete/", views.delete_smart_playlist, name="delete_smart_playlist"),
    path("smart-playlists/<int:pk>/thumbnail/", views.smart_playlist_thumb, name="smart_playlist_thumb"),
    path("smart-playlists/<int:pk>/thumbnail/generate/", views.smart_playlist_thumb_generate, name="smart_playlist_thumb_generate"),
    path("smart-playlists/<int:pk>/thumbnail/remove/", views.smart_playlist_thumb_remove, name="smart_playlist_thumb_remove"),
    path("smart-playlists/<int:pk>/thumbnail/upload/", views.smart_playlist_thumb_upload, name="smart_playlist_thumb_upload"),

    # Bulk actions
    path("bulk/thumb-regen/", views.bulk_regen_thumb, name="bulk_regen_thumb"),
    path("bulk/rating/", views.bulk_rating, name="bulk_rating"),
    path("bulk/playlist/", views.bulk_add_playlist, name="bulk_add_playlist"),
    path("bulk/favorite/", views.bulk_favorite, name="bulk_favorite"),
    path("bulk/hide/", views.bulk_hide, name="bulk_hide"),
    path("bulk/unhide/", views.bulk_unhide, name="bulk_unhide"),
    path("bulk/delete/", views.bulk_delete, name="bulk_delete"),
    path("api/playlists/", views.api_playlists, name="api_playlists"),

    # PWA
    path("manifest.json", views.pwa_manifest, name="pwa_manifest"),
    path("icons/<int:size>.png", views.pwa_icon, name="pwa_icon"),
]
