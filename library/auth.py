"""Auth pieces on top of django.contrib.auth: an owner/viewer role gate,
login throttling, and the login/logout views.

Role model: every account can log in and watch. Only accounts with
is_staff=True ("owner") may touch the filesystem or the library catalog --
scan, organize, rename, delete, convert, hide. Create owner accounts with
`createsuperuser`; create viewer accounts in /admin (Users -> Add, leave
"Staff status" unchecked).
"""
import functools

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.views import LoginView, LogoutView
from django.core.cache import cache
from django.http import JsonResponse
from django.utils.decorators import method_decorator

LOGIN_ATTEMPT_LIMIT = 10
LOGIN_ATTEMPT_WINDOW = 15 * 60  # seconds


def public_when_enabled(view_func):
    """Exempts a read-only view from login when HOMEFLIX_PUBLIC=1 (see
    settings.PUBLIC_ACCESS). Evaluated once at import time -- same as every
    other HOMEFLIX_* env var, this requires a process restart to take
    effect, which is already how the rest of the app's config works.
    Only apply this to views that are safe with request.user possibly being
    AnonymousUser -- see views._state_user() and UserPref.get/set, which are
    what make the per-user-state views (save_progress, the theme/autoplay/
    repeat/shuffle/seek-step toggles) degrade to a no-op instead of crashing."""
    if settings.PUBLIC_ACCESS:
        login_not_required(view_func)
    return view_func


def owner_required(view_func):
    """Blocks non-staff users. A JS-driven mutation (every one sends
    X-SPA -- see the `post()` helper in base.html) gets a JSON 403 the
    frontend can toast; anything else (a plain form POST, a typed URL)
    gets a small rendered page, so a stale/hostile request degrades
    gracefully instead of throwing a server error."""
    @functools.wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if request.user.is_authenticated and request.user.is_staff:
            return view_func(request, *args, **kwargs)
        if request.headers.get("X-SPA") == "1":
            return JsonResponse({"ok": False, "error": "Owner only"}, status=403)
        from .views import base_ctx
        from django.shortcuts import render
        return render(request, "library/forbidden.html", base_ctx(
            request, page_id="forbidden", spa_title="Not allowed — HomeFlix"),
            status=403)
    return wrapped


def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


@method_decorator(login_not_required, name="dispatch")
class ThrottledLoginView(LoginView):
    """Locks out an IP for LOGIN_ATTEMPT_WINDOW after LOGIN_ATTEMPT_LIMIT
    failed attempts. Counter lives in the filesystem cache (CACHES in
    settings.py) so it's shared across gunicorn workers."""
    template_name = "library/login.html"

    def _cache_key(self, request):
        return f"login-fails:{_client_ip(request)}"

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            fails = cache.get(self._cache_key(request), 0)
            if fails >= LOGIN_ATTEMPT_LIMIT:
                from django.shortcuts import render
                return render(request, "library/login.html", {
                    "form": self.get_form_class()(),
                    "throttled": True,
                }, status=429)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        cache.delete(self._cache_key(self.request))
        return super().form_valid(form)

    def form_invalid(self, form):
        key = self._cache_key(self.request)
        fails = cache.get(key, 0) + 1
        cache.set(key, fails, LOGIN_ATTEMPT_WINDOW)
        return super().form_invalid(form)


logout_view = LogoutView.as_view()
