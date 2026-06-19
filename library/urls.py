from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("library/", views.library, name="library"),
    path("history/", views.history, name="history"),
    path("playlists/", views.playlists, name="playlists"),
    path("playlists/<int:pk>/", views.playlist_detail, name="playlist_detail"),
    path("watch/<int:pk>/", views.watch, name="watch"),

    # media
    path("thumb/<int:pk>/", views.thumb, name="thumb"),
    path("stream/<int:pk>/", views.stream, name="stream"),
    path("frame/<int:pk>/<int:t>/", views.frame_thumb, name="frame_thumb"),

    # api
    path("api/videos/", views.api_videos, name="api_videos"),

    # conversion
    path("video/<int:pk>/convert/", views.convert, name="convert"),
    path("video/<int:pk>/convert/status/", views.convert_status, name="convert_status"),

    # organize / maintenance
    path("organize/", views.organize, name="organize"),
    path("purge-missing/", views.purge_missing, name="purge_missing"),
    path("reset-library/", views.reset_library, name="reset_library"),

    # actions
    path("scan/", views.scan, name="scan"),
    path("video/<int:pk>/thumb/regen/", views.regen_thumb, name="regen_thumb"),
    path("video/<int:pk>/progress/", views.save_progress, name="save_progress"),
    path("video/<int:pk>/favorite/", views.toggle_favorite, name="toggle_favorite"),
    path("video/<int:pk>/rating/", views.set_rating, name="set_rating"),
    path("video/<int:pk>/playlist/", views.add_to_playlist, name="add_to_playlist"),
    path("theme/", views.toggle_theme, name="toggle_theme"),
    path("random/", views.random_video, name="random_video"),
    path("video/<int:pk>/notes/", views.add_note, name="add_note"),
    path("video/<int:pk>/notes/<int:note_pk>/delete/", views.delete_note, name="delete_note"),
    path("autoplay/", views.toggle_autoplay, name="toggle_autoplay"),
    path("playlists/create/", views.create_playlist, name="create_playlist"),
    path("playlists/<int:pk>/delete/", views.delete_playlist, name="delete_playlist"),
    path("smart-playlists/create/", views.create_smart_playlist, name="create_smart_playlist"),
    path("smart-playlists/<int:pk>/", views.smart_playlist_detail, name="smart_playlist_detail"),
    path("smart-playlists/<int:pk>/rules/", views.save_smart_rules, name="save_smart_rules"),
    path("smart-playlists/<int:pk>/delete/", views.delete_smart_playlist, name="delete_smart_playlist"),
]
