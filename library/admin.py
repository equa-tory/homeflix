from django.contrib import admin
from .models import Video, Tag, Playlist, PlaylistItem, PlaybackState, WatchEvent, Setting


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = ("title", "ext", "aspect_label", "favorite", "rating", "date_added", "missing", "hidden")
    list_filter = ("ext", "favorite", "missing", "hidden", "browser_playable")
    search_fields = ("title", "filename", "rel_path")
    filter_horizontal = ("tags",)


admin.site.register([Tag, Playlist, PlaylistItem, PlaybackState, WatchEvent, Setting])
