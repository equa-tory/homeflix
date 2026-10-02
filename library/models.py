from django.conf import settings
from django.db import models


class Tag(models.Model):
    name = models.CharField(max_length=80, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Video(models.Model):
    # File identity
    file_path = models.CharField(max_length=1024, unique=True)   # absolute path on disk
    rel_path = models.CharField(max_length=1024, db_index=True)  # path relative to library root
    filename = models.CharField(max_length=512)
    ext = models.CharField(max_length=16, db_index=True)         # mp4, webm, mkv ...

    # Display
    title = models.CharField(max_length=512)
    description = models.TextField(blank=True, default="")

    # Technical (from ffprobe)
    duration_seconds = models.FloatField(null=True, blank=True)
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    video_codec = models.CharField(max_length=32, blank=True, default="")
    audio_codec = models.CharField(max_length=32, blank=True, default="")
    size_bytes = models.BigIntegerField(null=True, blank=True)

    # Whether <video> can play this directly in a desktop browser.
    # MKV containers and HEVC/H.265 are flagged False -> conversion candidates.
    browser_playable = models.BooleanField(default=True)

    # On-demand conversion to a browser-friendly MP4 (for MKV / HEVC etc.)
    CONVERT_NONE, CONVERT_QUEUED, CONVERT_RUNNING, CONVERT_DONE, CONVERT_FAILED = (
        "none", "queued", "running", "done", "failed")
    convert_status = models.CharField(max_length=12, default=CONVERT_NONE)
    converted_path = models.CharField(max_length=1024, blank=True, default="")
    convert_progress = models.PositiveSmallIntegerField(default=0)  # 0-100

    @property
    def needs_conversion(self):
        return not self.browser_playable and self.convert_status != self.CONVERT_DONE

    @property
    def needs_convert_ui(self):
        """Whether the UI should show the 'MKV/HEVC, convert?' nudge. Non-native
        files still play live via HLS regardless — the manual Convert button
        just bakes a permanent/downloadable MP4 copy."""
        return self.needs_conversion

    @property
    def playable_now(self):
        """True if the browser can play it directly, a converted copy is ready,
        or (for anything else) it will be streamed live via HLS."""
        return True

    # Thumbnail
    thumbnail_path = models.CharField(max_length=1024, blank=True, default="")
    thumbnail_percent = models.FloatField(default=0.0)

    # yt-dlp sidecar metadata (optional)
    source_url = models.URLField(blank=True, default="")
    channel = models.CharField(max_length=256, blank=True, default="")
    upload_date = models.DateField(null=True, blank=True)

    # User state
    favorite = models.BooleanField(default=False)
    rating = models.PositiveSmallIntegerField(default=0)  # 0 = unrated, 1-5
    tags = models.ManyToManyField(Tag, blank=True, related_name="videos")

    # Timestamps
    file_mtime = models.DateTimeField(null=True, blank=True)
    date_added = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    missing = models.BooleanField(default=False)

    # User-hidden (not deleted): excluded from Home/Library/search/shorts/random,
    # only listed on the Hidden page so it can be unhidden or deleted later.
    hidden = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-date_added"]

    def __str__(self):
        return self.title

    @property
    def aspect_label(self):
        if self.width and self.height:
            if self.height >= 2160:
                return "4K"
            if self.height >= 1080:
                return "1080p"
            if self.height >= 720:
                return "720p"
            return f"{self.height}p"
        return ""

    @property
    def is_portrait(self):
        """Vertical video. Square (w == h) counts as vertical on purpose --
        it frames far better in the shorts/vertical player than in 16:9.
        SQL-side twin: views.PORTRAIT_Q -- keep the two in sync."""
        return bool(self.width and self.height and self.height >= self.width)


class VideoSubtitle(models.Model):
    """User-uploaded subtitle attached to a video, stored as app-owned WebVTT.
    Decoupled from disk on purpose -- the original .ass/.srt can be moved
    (organize) or deleted by the user later and this keeps working."""
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="uploaded_subtitles")
    label = models.CharField(max_length=120)
    lang = models.CharField(max_length=16, blank=True, default="")
    vtt_path = models.CharField(max_length=1024)   # absolute path in SUBTITLE_DIR
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.video.title}: {self.label}"


class PlaybackState(models.Model):
    """Resume position. One row per (video, user) so each account's progress
    is private. `user` is nullable so pre-auth rows (and any edge case where
    a row somehow gets created without a request.user) never hard-fail the
    migration -- they just become invisible to every user-scoped query."""
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="playback_states")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              null=True, blank=True, related_name="playback_states")
    position_seconds = models.FloatField(default=0.0)
    finished = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("video", "user")


class WatchEvent(models.Model):
    """Logged each time a video is watched, for the History page. Per-user,
    same nullable-FK reasoning as PlaybackState above."""
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="watch_events")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              null=True, blank=True, related_name="watch_events")
    watched_at = models.DateTimeField(auto_now_add=True)
    progress_seconds = models.FloatField(default=0.0)

    class Meta:
        ordering = ["-watched_at"]


class Playlist(models.Model):
    name = models.CharField(max_length=256)
    created = models.DateTimeField(auto_now_add=True)
    videos = models.ManyToManyField(Video, through="PlaylistItem", related_name="playlists")

    # Generated (2x2 collage of its videos' thumbnails) or user-uploaded cover image.
    thumbnail_path = models.CharField(max_length=1024, blank=True, default="")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class PlaylistItem(models.Model):
    playlist = models.ForeignKey(Playlist, on_delete=models.CASCADE, related_name="items")
    video = models.ForeignKey(Video, on_delete=models.CASCADE)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        unique_together = ("playlist", "video")


class Setting(models.Model):
    """Key/value store for app preferences (dark mode, library path)."""
    key = models.CharField(max_length=64, unique=True)
    value = models.CharField(max_length=1024, blank=True, default="")

    @classmethod
    def get(cls, key, default=""):
        row = cls.objects.filter(key=key).first()
        return row.value if row else default

    @classmethod
    def set(cls, key, value):
        cls.objects.update_or_create(key=key, defaults={"value": str(value)})


class UserPref(models.Model):
    """Per-user key/value store (theme, autoplay, repeat, shuffle) -- the
    per-user twin of Setting. Kept as a separate model rather than adding a
    user FK to Setting because Setting is also read from the background
    scanner (services.scan_library) where there is no request/user."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="prefs")
    key = models.CharField(max_length=64)
    value = models.CharField(max_length=1024, blank=True, default="")

    class Meta:
        unique_together = ("user", "key")

    @classmethod
    def get(cls, user, key, default=""):
        if not user or not user.is_authenticated:
            return default
        row = cls.objects.filter(user=user, key=key).first()
        return row.value if row else default

    @classmethod
    def set(cls, user, key, value):
        # Mirrors the get() guard above -- an anonymous visitor (HOMEFLIX_PUBLIC
        # mode) has no account to persist to; silently no-op rather than crash
        # (AnonymousUser is not a real User row, so assigning it to the `user`
        # FK raises ValueError).
        if not user or not user.is_authenticated:
            return
        cls.objects.update_or_create(user=user, key=key, defaults={"value": str(value)})


class VideoNote(models.Model):
    """Timestamped note on a video. Clicking the timestamp seeks there."""
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="notes")
    timestamp_seconds = models.FloatField(default=0.0)
    text = models.TextField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["timestamp_seconds"]

    def __str__(self):
        return f"{self.video.title} @ {self.timestamp_seconds:.0f}s"


class SmartPlaylist(models.Model):
    """Auto-populating playlist defined by filter rules (stored as JSON)."""
    name = models.CharField(max_length=256)
    created = models.DateTimeField(auto_now_add=True)
    # JSON list of {"field","op","value"} dicts
    rules = models.TextField(default="[]")

    # Generated (2x2 collage of its videos' thumbnails) or user-uploaded cover image.
    thumbnail_path = models.CharField(max_length=1024, blank=True, default="")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"Smart: {self.name}"

    def get_videos(self):
        import json
        qs = Video.objects.filter(missing=False)
        try:
            rules = json.loads(self.rules)
        except Exception:
            return qs.none()
        for r in rules:
            f, op, val = r.get("field", ""), r.get("op", ""), r.get("value", "")
            try:
                if   f=="title"            and op=="contains":     qs=qs.filter(title__icontains=val)
                elif f=="title"            and op=="not_contains":  qs=qs.exclude(title__icontains=val)
                elif f=="filename"         and op=="contains":     qs=qs.filter(filename__icontains=val)
                elif f=="channel"          and op=="contains":     qs=qs.filter(channel__icontains=val)
                elif f=="tags"             and op=="is":           qs=qs.filter(tags__name__iexact=val)
                elif f=="ext"              and op=="is":           qs=qs.filter(ext=val.lower().lstrip("."))
                elif f=="favorite"         and op=="is":           qs=qs.filter(favorite=True)
                elif f=="rating"           and op=="gte":          qs=qs.filter(rating__gte=int(val))
                elif f=="rating"           and op=="lte":          qs=qs.filter(rating__lte=int(val))
                elif f=="duration_seconds" and op=="gte":          qs=qs.filter(duration_seconds__gte=float(val))
                elif f=="duration_seconds" and op=="lte":          qs=qs.filter(duration_seconds__lte=float(val))
                elif f=="height"           and op=="gte":          qs=qs.filter(height__gte=int(val))
                elif f=="height"           and op=="is":
                    h=int(val)
                    qs=qs.filter(height__gte=h, height__lt=h*2)   # e.g. "1080" matches 1080p
            except (ValueError, TypeError):
                pass
        return qs.distinct().order_by("-date_added")


class DownloadSource(models.Model):
    """A saved YouTube playlist link the owner pulls undownloaded videos from
    (see library/downloader.py). `entries_json` caches the last fetch so the
    Downloads page can re-render without hitting YouTube every time."""
    url = models.URLField(max_length=1024)
    name = models.CharField(max_length=256, blank=True, default="")
    # Relative to LIBRARY_ROOT; blank = the library root itself. Validated in
    # downloader.resolve_target() so it can never escape the root.
    target_subdir = models.CharField(max_length=512, blank=True, default="")
    last_fetched = models.DateTimeField(null=True, blank=True)
    entries_json = models.TextField(blank=True, default="[]")
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name or self.url


class SkippedEntry(models.Model):
    """A playlist video the owner marked "already downloaded" -- it is never
    offered for download again. DB-only on purpose: Lasso's archive file is
    left alone, and Unskip just deletes the row."""
    source = models.ForeignKey(DownloadSource, on_delete=models.CASCADE, related_name="skipped")
    video_id = models.CharField(max_length=32)
    title = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        unique_together = ("source", "video_id")


class DownloadJob(models.Model):
    """One yt-dlp run over a hand-picked set of playlist items. State lives
    here (plus the pid), not in process memory, so status polling and cancel
    work from any gunicorn worker -- same reasoning as the HLS sessions."""
    QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
    source = models.ForeignKey(DownloadSource, on_delete=models.CASCADE, related_name="jobs")
    ids = models.TextField(default="[]")              # JSON list of video ids
    status = models.CharField(max_length=12, default=QUEUED)
    pid = models.IntegerField(null=True, blank=True)
    total = models.PositiveIntegerField(default=0)
    done_count = models.PositiveIntegerField(default=0)
    fail_count = models.PositiveIntegerField(default=0)
    current_title = models.CharField(max_length=512, blank=True, default="")
    current_pct = models.PositiveSmallIntegerField(default=0)
    summary = models.CharField(max_length=256, blank=True, default="")
    log_tail = models.TextField(blank=True, default="")
    started = models.DateTimeField(auto_now_add=True)
    finished = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
