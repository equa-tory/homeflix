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
    def needs_remux(self):
        """True if only the *container* is the problem (e.g. h264/aac wrapped
        in mkv) — those can be streamed live via a fast lossless ffmpeg remux
        instead of writing a full converted copy to disk. Real codec problems
        (HEVC etc.) still need the on-disk Convert flow (needs_conversion)."""
        from django.conf import settings
        if not self.needs_conversion:
            return False
        if f".{(self.ext or '').lower()}" not in settings.NON_BROWSER_CONTAINERS:
            return False
        return ((self.video_codec or "").lower() in settings.REMUX_SAFE_VCODECS
                and (self.audio_codec or "").lower() in settings.REMUX_SAFE_ACODECS)

    @property
    def needs_convert_ui(self):
        """Whether the UI should show the 'MKV/HEVC, convert?' nudge — false
        for videos handled transparently via live remux (needs_remux), since
        those already just play with no action needed."""
        return self.needs_conversion and not self.needs_remux

    @property
    def playable_now(self):
        """True if the browser can play it directly, a converted copy is
        ready, or it can be streamed live via remux."""
        return self.browser_playable or self.convert_status == self.CONVERT_DONE or self.needs_remux

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


class PlaybackState(models.Model):
    """Resume position. One row per video (single user -> global = synced)."""
    video = models.OneToOneField(Video, on_delete=models.CASCADE, related_name="playback")
    position_seconds = models.FloatField(default=0.0)
    finished = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)


class WatchEvent(models.Model):
    """Logged each time a video is watched, for the History page."""
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="watch_events")
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
