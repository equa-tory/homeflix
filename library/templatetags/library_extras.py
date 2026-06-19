from django import template

register = template.Library()


@register.filter
def duration(seconds):
    if not seconds:
        return "--:--"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


@register.filter
def filesize(num_bytes):
    if not num_bytes:
        return ""
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


@register.filter
def pct(state, video):
    """Resume progress as a percentage for the thumbnail bar."""
    try:
        if state and video.duration_seconds:
            return min(100, (state.position_seconds / video.duration_seconds) * 100)
    except (ZeroDivisionError, AttributeError):
        pass
    return 0
