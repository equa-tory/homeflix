"""The one icon set.

Every icon in the UI is an inline SVG drawn from this table, so a button looks
the same on a PC, a phone and a TV -- emoji and symbol characters (🎲 ▶ ⚠ …)
render differently (or as colour emoji) depending on the device's fonts.

Each value is the *inner* markup of a 24x24 `<symbol>`. Strokes/fills come from
CSS (`svg.i` in base.html): 2px round-capped outline in `currentColor`; add the
class `fill` to an icon to paint it solid (liked / favorite / subscribed). A few
shapes (play, pause, dots) are always solid and say so with `fill="currentColor"`.

`sprite()` renders the hidden `<svg>` that base.html includes once; `svg(name)`
renders a use-reference; the page's JS has the same two via `ic(name)`. Names are
checked by tests (every icon referenced in a template or script must exist here).
"""
from django.utils.html import format_html
from django.utils.safestring import mark_safe

_DOT = '<circle cx="{}" cy="{}" r="1.6" fill="currentColor" stroke="none"/>'


def _dots(*pts):
    return "".join(_DOT.format(x, y) for x, y in pts)


ICONS = {
    # --- navigation ---------------------------------------------------------
    "home": '<path d="M3 11.5 12 4l9 7.5"/><path d="M5 10v10h5v-6h4v6h5V10"/>',
    "library": '<rect x="3" y="3" width="7.5" height="7.5" rx="1.6"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.6"/>'
               '<rect x="3" y="13.5" width="7.5" height="7.5" rx="1.6"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.6"/>',
    "channels": '<rect x="3" y="8" width="18" height="13" rx="2.2"/><path d="m8 3 4 5 4-5"/>',
    "playlists": '<path d="M9 6h12M9 12h12M9 18h12"/>' + _dots((4.5, 6), (4.5, 12), (4.5, 18)),
    "history": '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7.5V12l3 2"/>',
    "shorts": '<rect x="6.5" y="2.5" width="11" height="19" rx="2.6"/><path d="M10.5 18.5h3"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.4-4.4"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "moon": '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
    "login": '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><path d="m10 17 5-5-5-5M15 12H3"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5M21 12H9"/>',
    # --- manage menu --------------------------------------------------------
    "refresh": '<path d="M21 12a9 9 0 0 1-15.5 6.2L3 16"/><path d="M3 21v-5h5"/><path d="M3 12a9 9 0 0 1 15.5-6.2L21 8"/><path d="M21 3v5h-5"/>',
    "layers": '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>',
    "download": '<path d="M12 3v12M7 10l5 5 5-5M4 21h16"/>',
    "folder": '<path d="M3 6.5A2.5 2.5 0 0 1 5.5 4H9l2 3h7.5A2.5 2.5 0 0 1 21 9.5v8A2.5 2.5 0 0 1 18.5 20h-13A2.5 2.5 0 0 1 3 17.5z"/>',
    "save": '<path d="M5 3h11l3 3v15H5z"/><path d="M8 3v5h7V3M8 21v-7h8v7"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2.4"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/>',
    "eraser": '<path d="m7 21-4.3-4.3a2.4 2.4 0 0 1 0-3.4l9.6-9.6a2.4 2.4 0 0 1 3.4 0l5.6 5.6a2.4 2.4 0 0 1 0 3.4L13 21"/><path d="M22 21H7M5 11l9 9"/>',
    "reset": '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
    "sliders": '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    "external": '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14 21 3"/>',
    "wrench": '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.8-3.8a6 6 0 0 1-7.9 7.9l-6.9 6.9a2.1 2.1 0 0 1-3-3l6.9-6.9a6 6 0 0 1 7.9-7.9z"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "convert": '<path d="M8 3 4 7l4 4M4 7h16M16 21l4-4-4-4M20 17H4"/>',
    "pencil": '<path d="M17 3a2.8 2.8 0 0 1 4 4L7.5 20.5 2 22l1.5-5.5z"/>',
    # --- reactions ----------------------------------------------------------
    "thumb-up": '<path d="M7 10v12"/><path d="M15 5.9 14 10h5.8a2 2 0 0 1 1.9 2.6l-2.3 8a2 2 0 0 1-1.9 1.4H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.8a2 2 0 0 0 1.8-1.1L12 2a3.1 3.1 0 0 1 3 3.9z"/>',
    "thumb-down": '<path d="M17 14V2"/><path d="M9 18.1 10 14H4.2a2 2 0 0 1-1.9-2.6l2.3-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.8a2 2 0 0 0-1.8 1.1L12 22a3.1 3.1 0 0 1-3-3.9z"/>',
    "heart": '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1 1.1L12 21l7.8-7.5 1-1.1a5.5 5.5 0 0 0 0-7.8z"/>',
    "bell": '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
    "unseen": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.2" fill="currentColor" stroke="none"/>',
    "eye": '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M9.9 4.2A10 10 0 0 1 12 4c6.4 0 10 8 10 8a17 17 0 0 1-3.2 4.3M6.6 6.6A17 17 0 0 0 2 12s3.6 8 10 8a9.7 9.7 0 0 0 5.4-1.6"/><path d="M14.1 14.1a3 3 0 0 1-4.2-4.2M2 2l20 20"/>',
    "trash": '<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6"/>',
    # --- player -------------------------------------------------------------
    "play": '<path d="M7 4.5v15a1 1 0 0 0 1.5.9l12-7.5a1 1 0 0 0 0-1.8l-12-7.5A1 1 0 0 0 7 4.5z" fill="currentColor"/>',
    "pause": '<rect x="6" y="4" width="4.2" height="16" rx="1.2" fill="currentColor"/><rect x="13.8" y="4" width="4.2" height="16" rx="1.2" fill="currentColor"/>',
    "skip-back": '<path d="M19 5.2v13.6a.8.8 0 0 1-1.2.7L8 13a1.2 1.2 0 0 1 0-2l9.8-6.5A.8.8 0 0 1 19 5.2z" fill="currentColor"/><path d="M5 5v14"/>',
    "skip-forward": '<path d="M5 5.2v13.6a.8.8 0 0 0 1.2.7L16 13a1.2 1.2 0 0 0 0-2L6.2 4.5A.8.8 0 0 0 5 5.2z" fill="currentColor"/><path d="M19 5v14"/>',
    "volume": '<path d="M11 5 6 9H3v6h3l5 4z" fill="currentColor"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/>',
    "volume-low": '<path d="M11 5 6 9H3v6h3l5 4z" fill="currentColor"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/>',
    "volume-off": '<path d="M11 5 6 9H3v6h3l5 4z" fill="currentColor"/><path d="m22 9-6 6M16 9l6 6"/>',
    "fullscreen": '<path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/>',
    "expand": '<path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/>',
    "shuffle": '<path d="M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5"/>',
    "repeat": '<path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/>',
    "repeat-one": '<path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/><path d="M11 10h1.2v4.6"/>',
    "dice": '<rect x="3" y="3" width="18" height="18" rx="3.4"/>' + _dots((8.2, 8.2), (15.8, 8.2), (12, 12), (8.2, 15.8), (15.8, 15.8)),
    "autoplay": '<path d="M3 5.5v13a.8.8 0 0 0 1.2.7l8-6.5a.8.8 0 0 0 0-1.2l-8-6.5A.8.8 0 0 0 3 5.5z" fill="currentColor"/><path d="M13 5.5v13a.8.8 0 0 0 1.2.7l8-6.5a.8.8 0 0 0 0-1.2l-8-6.5a.8.8 0 0 0-1.2.7z" fill="currentColor"/>',
    "wide": '<rect x="2" y="6" width="20" height="12" rx="2.4"/>',
    "list-add": '<path d="M11 12H3M16 6H3M16 18H3M18 9v6M21 12h-6"/>',
    "more": _dots((5, 12), (12, 12), (19, 12)),
    "grip": _dots((9, 6), (15, 6), (9, 12), (15, 12), (9, 18), (15, 18)),
    # --- generic ------------------------------------------------------------
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "check": '<path d="m20 6-11 11-5-5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "chevron-left": '<path d="m15 6-6 6 6 6"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "arrow-up": '<path d="M12 19V5M5 12l7-7 7 7"/>',
    "arrow-down": '<path d="M12 5v14M19 12l-7 7-7-7"/>',
    "arrow-right": '<path d="M5 12h14M12 5l7 7-7 7"/>',
    "sort": '<path d="m7 15 5 5 5-5M7 9l5-5 5 5"/>',
    "select": '<path d="m9 11 3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
    "alert": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    "film": '<rect x="2" y="2" width="20" height="20" rx="2.4"/><path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 17h5M17 7h5"/>',
}


def sprite():
    """The hidden <svg> holding every symbol; included once in base.html."""
    body = "".join(f'<symbol id="i-{name}" viewBox="0 0 24 24">{inner}</symbol>'
                   for name, inner in ICONS.items())
    return mark_safe(f'<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0" '
                     f'style="position:absolute" aria-hidden="true" focusable="false">{body}</svg>')


def svg(name, extra=""):
    """A use-reference to one symbol. Unknown names fail loudly (KeyError) so a
    typo is caught by the tests instead of shipping an invisible button."""
    ICONS[name]
    cls = f"i {extra}".strip()
    return format_html('<svg class="{}" aria-hidden="true" focusable="false"><use href="#i-{}"/></svg>', cls, name)
