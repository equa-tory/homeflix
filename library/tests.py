"""Auth/permission/per-user-state tests.

This app has no test coverage otherwise (see CLAUDE.md), but the
owner/viewer split added on top of django.contrib.auth is exactly the kind
of thing that silently regresses -- a new view added without @owner_required,
or a query that joins the wrong user's PlaybackState row -- so it gets real
tests instead of just manual verification.
"""
import os
import tempfile

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from . import views
from .auth import LOGIN_ATTEMPT_LIMIT, public_when_enabled
from .models import PlaybackState, Playlist, PlaylistItem, UserPref, Video, WatchEvent

User = get_user_model()

# Owner-only endpoints (see the @owner_required table in views.py / the plan
# this was built from). Kept here as an explicit list -- rather than
# introspecting urls.py -- so a reviewer can see at a glance which surface
# this test suite is asserting is locked down.
OWNER_POST_NO_PK = [
    "scan", "set_default_thumb_percent", "purge_missing", "reset_library",
    "bulk_regen_thumb", "bulk_rename", "bulk_hide", "bulk_unhide", "bulk_delete",
]
OWNER_POST_PK = [
    "regen_thumb", "rename_video", "toggle_hidden", "delete_video",
    "convert", "cancel_convert",
]
OWNER_GET_NO_PK = ["hidden_videos", "organize", "duplicates"]


def _make_video(**kw):
    """A Video row backed by a real (empty) temp file, so views that check
    os.path.exists() or shell out to ffprobe/ffmpeg on the file degrade
    gracefully (nonzero exit, caught by services._run) instead of behaving
    unrealistically differently from a real deployment."""
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    defaults = dict(
        file_path=path, rel_path=os.path.basename(path),
        filename=os.path.basename(path), ext="mp4", title="Test Video",
        missing=False, hidden=False,
    )
    defaults.update(kw)
    return Video.objects.create(**defaults)


@override_settings(LIBRARY_ROOT=tempfile.mkdtemp())
class AuthTestCase(TestCase):
    """Pins LIBRARY_ROOT to an empty scratch dir for every test in this
    module, regardless of the developer's real HOMEFLIX_LIBRARY env var --
    without this, the owner-allowed test below (which POSTs to /scan/) would
    walk and ffprobe someone's real, possibly huge, video library."""

    def setUp(self):
        self.owner = User.objects.create_user("owner", password="pw-owner-1234", is_staff=True)
        self.viewer = User.objects.create_user("viewer", password="pw-viewer-1234", is_staff=False)
        self.video = _make_video()
        cache.clear()

    def tearDown(self):
        for v in Video.objects.all():
            if v.file_path and os.path.exists(v.file_path):
                os.remove(v.file_path)


class LoginRequiredTests(AuthTestCase):
    """Default-deny: LoginRequiredMiddleware must cover every ordinary page,
    not just the ones someone remembered to check by hand."""

    def test_anonymous_redirected_to_login(self):
        protected = [
            reverse("home"), reverse("library"), reverse("playlists"),
            reverse("history"), reverse("shorts"),
            reverse("watch", args=[self.video.pk]),
            reverse("stream", args=[self.video.pk]),
            reverse("thumb", args=[self.video.pk]),
            reverse("api_videos"),
            reverse("hidden_videos"),
        ]
        for url in protected:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 302)
                self.assertIn(reverse("login"), resp.url)

    def test_anonymous_post_also_redirected(self):
        resp = self.client.post(reverse("save_progress", args=[self.video.pk]), {"position": "5"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp.url)

    def test_pwa_endpoints_exempt_from_login(self):
        self.assertEqual(self.client.get(reverse("pwa_manifest")).status_code, 200)
        self.assertEqual(self.client.get(reverse("pwa_icon", args=[32])).status_code, 200)


class OwnerOnlyTests(AuthTestCase):
    def test_viewer_blocked_from_owner_post_no_pk_endpoints(self):
        self.client.force_login(self.viewer)
        for name in OWNER_POST_NO_PK:
            with self.subTest(view=name):
                resp = self.client.post(reverse(name), {}, HTTP_X_SPA="1")
                self.assertEqual(resp.status_code, 403, name)

    def test_viewer_blocked_from_owner_post_pk_endpoints(self):
        self.client.force_login(self.viewer)
        for name in OWNER_POST_PK:
            with self.subTest(view=name):
                resp = self.client.post(reverse(name, args=[self.video.pk]), {}, HTTP_X_SPA="1")
                self.assertEqual(resp.status_code, 403, name)

    def test_viewer_blocked_from_owner_get_endpoints(self):
        self.client.force_login(self.viewer)
        for name in OWNER_GET_NO_PK:
            with self.subTest(view=name):
                resp = self.client.get(reverse(name))
                self.assertEqual(resp.status_code, 403, name)

    def test_owner_allowed_on_owner_endpoints(self):
        self.client.force_login(self.owner)
        for name in OWNER_GET_NO_PK:
            with self.subTest(view=name):
                resp = self.client.get(reverse(name))
                self.assertNotEqual(resp.status_code, 403, name)
        for name in OWNER_POST_NO_PK:
            with self.subTest(view=name):
                resp = self.client.post(reverse(name), {}, HTTP_X_SPA="1")
                self.assertNotEqual(resp.status_code, 403, name)

    def test_bulk_delete_file_mode_blocked_for_viewer_leaves_file_on_disk(self):
        self.client.force_login(self.viewer)
        path = self.video.file_path
        resp = self.client.post(reverse("bulk_delete"),
                                 {"ids": str(self.video.pk), "mode": "file"}, HTTP_X_SPA="1")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(os.path.exists(path), "owner_required must run before any file is touched")
        self.assertTrue(Video.objects.filter(pk=self.video.pk).exists())

    def test_organize_move_blocked_for_viewer(self):
        self.client.force_login(self.viewer)
        resp = self.client.post(reverse("organize"), {"confirm": "1"}, HTTP_X_SPA="1")
        self.assertEqual(resp.status_code, 403)

    def test_viewer_keeps_non_owner_actions(self):
        """Sanity check the split isn't over-broad: everyday viewer actions
        (favorite, rating, playlists, progress) must still work."""
        self.client.force_login(self.viewer)
        resp = self.client.post(reverse("toggle_favorite", args=[self.video.pk]), {}, HTTP_X_SPA="1")
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(reverse("save_progress", args=[self.video.pk]), {"position": "12"})
        self.assertEqual(resp.status_code, 200)


class HiddenVideoTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.hidden_video = _make_video(hidden=True, file_path=self.video.file_path.replace(".mp4", "-h.mp4"))
        open(self.hidden_video.file_path, "wb").close()

    def test_viewer_gets_404_on_hidden_video(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("watch", args=[self.hidden_video.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("stream", args=[self.hidden_video.pk])).status_code, 404)

    def test_owner_can_reach_hidden_video(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("watch", args=[self.hidden_video.pk]))
        self.assertEqual(resp.status_code, 200)


class PerUserStateTests(AuthTestCase):
    def test_progress_is_isolated_per_user(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("save_progress", args=[self.video.pk]), {"position": "30"})
        self.client.logout()

        self.client.force_login(self.viewer)
        self.client.post(reverse("save_progress", args=[self.video.pk]), {"position": "99"})

        owner_state = PlaybackState.objects.get(video=self.video, user=self.owner)
        viewer_state = PlaybackState.objects.get(video=self.video, user=self.viewer)
        self.assertEqual(owner_state.position_seconds, 30.0)
        self.assertEqual(viewer_state.position_seconds, 99.0)

    def test_finished_video_excluded_only_for_the_user_who_finished_it(self):
        long_video = _make_video(duration_seconds=100.0, title="Long")
        self.client.force_login(self.owner)
        self.client.post(reverse("save_progress", args=[long_video.pk]), {"position": "95"})
        self.assertTrue(PlaybackState.objects.get(video=long_video, user=self.owner).finished)

        # A different user hasn't watched it -- it must still be eligible
        # for their Home page discovery/recommendation rows.
        self.assertFalse(
            PlaybackState.objects.filter(video=long_video, user=self.viewer).exists())

    def test_watch_history_is_isolated_per_user(self):
        self.client.force_login(self.owner)
        self.client.get(reverse("watch", args=[self.video.pk]))
        self.client.logout()

        self.client.force_login(self.viewer)
        self.client.get(reverse("watch", args=[self.video.pk]))

        self.assertEqual(WatchEvent.objects.filter(user=self.owner, video=self.video).count(), 1)
        self.assertEqual(WatchEvent.objects.filter(user=self.viewer, video=self.video).count(), 1)

    def test_theme_preference_is_isolated_per_user(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("toggle_theme"), {}, HTTP_X_SPA="1")
        self.client.logout()

        self.client.force_login(self.viewer)
        # Viewer never toggled -- must still see the default, not the
        # owner's theme.
        self.assertEqual(UserPref.get(self.viewer, "theme", "dark"), "dark")
        self.assertEqual(UserPref.get(self.owner, "theme", "dark"), "light")


class LoginThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        User.objects.create_user("someone", password="correct-horse-battery")

    def test_lockout_after_repeated_failures(self):
        login_url = reverse("login")
        for _ in range(LOGIN_ATTEMPT_LIMIT):
            resp = self.client.post(login_url, {"username": "someone", "password": "wrong"})
            self.assertEqual(resp.status_code, 200)  # re-rendered login form
        # One more failure past the limit -- now throttled.
        resp = self.client.post(login_url, {"username": "someone", "password": "wrong"})
        self.assertEqual(resp.status_code, 429)

    def test_successful_login_not_throttled(self):
        login_url = reverse("login")
        resp = self.client.post(login_url, {"username": "someone", "password": "correct-horse-battery"})
        self.assertEqual(resp.status_code, 302)


class PublicAccessDecoratorTests(TestCase):
    """public_when_enabled (library/auth.py) is applied to each public-
    capable view exactly once, when views.py is first imported -- i.e. once
    per process, at Django startup, using whatever HOMEFLIX_PUBLIC was set
    to then. That means override_settings(PUBLIC_ACCESS=...) in a test
    cannot toggle the *already-decorated* views.home etc. (the decorator
    already ran); it can only be exercised by calling the decorator itself,
    which is what these tests do. This mirrors exactly how every other
    HOMEFLIX_* env var already works (read once at process start) -- see
    config/settings.py."""

    def test_marks_view_exempt_when_public_access_on(self):
        def dummy(request):
            pass
        with override_settings(PUBLIC_ACCESS=True):
            public_when_enabled(dummy)
        self.assertFalse(dummy.login_required)

    def test_leaves_view_untouched_when_public_access_off(self):
        def dummy(request):
            pass
        with override_settings(PUBLIC_ACCESS=False):
            public_when_enabled(dummy)
        # No attribute was set at all -- LoginRequiredMiddleware's own
        # getattr(view_func, "login_required", True) default applies.
        self.assertFalse(hasattr(dummy, "login_required"))


class AnonymousSafeQueryTests(AuthTestCase):
    """Exercises the actual view functions with an AnonymousUser request --
    bypassing the URL/login-required dispatch entirely (this calls the
    Python functions directly), which is the only way to test this
    independently of whatever HOMEFLIX_PUBLIC happened to be at process
    start (see PublicAccessDecoratorTests above). This is the crash these
    tests guard against: AnonymousUser is not a real User row, so handing it
    to a PlaybackState/WatchEvent/UserPref query raises -- confirmed
    directly against these models during development:
        filter(playback_states__user=AnonymousUser()) -> TypeError
        PlaybackState(user=AnonymousUser())            -> ValueError
    _state_user()/UserPref's anonymous guards exist specifically to avoid
    that; these tests would fail with a 500-shaped exception if a future
    change reintroduces a bare `request.user` into one of these queries."""

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()

    def _anon_get(self, path):
        request = self.factory.get(path)
        request.user = AnonymousUser()
        return request

    def test_home_view_does_not_crash_for_anonymous(self):
        request = self._anon_get(reverse("home"))
        resp = views.home(request)
        self.assertEqual(resp.status_code, 200)

    def test_watch_api_does_not_crash_and_writes_no_state_for_anonymous(self):
        request = self._anon_get(reverse("watch_api", args=[self.video.pk]))
        resp = views.watch_api(request, self.video.pk)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(PlaybackState.objects.filter(video=self.video).exists())
        self.assertFalse(WatchEvent.objects.filter(video=self.video).exists())

    def test_save_progress_noops_for_anonymous(self):
        request = self.factory.post(
            reverse("save_progress", args=[self.video.pk]), {"position": "42"})
        request.user = AnonymousUser()
        resp = views.save_progress(request, self.video.pk)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(PlaybackState.objects.filter(video=self.video).exists())

    def test_userpref_set_noops_for_anonymous(self):
        UserPref.set(AnonymousUser(), "theme", "light")
        self.assertEqual(UserPref.objects.count(), 0)

    def test_attach_playback_handles_anonymous(self):
        videos = views._attach_playback([self.video], AnonymousUser())
        self.assertEqual(videos[0].my_playback, [])

    def test_random_video_does_not_crash_for_anonymous(self):
        request = self._anon_get(reverse("random_video") + "?json=1")
        resp = views.random_video(request)
        self.assertEqual(resp.status_code, 200)

    def test_toggle_theme_noops_for_anonymous(self):
        request = self.factory.post(reverse("toggle_theme"))
        request.user = AnonymousUser()
        resp = views.toggle_theme(request)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(UserPref.objects.count(), 0)

    def test_set_seek_step_noops_for_anonymous(self):
        request = self.factory.post(reverse("set_seek_step"), {"value": "20"})
        request.user = AnonymousUser()
        resp = views.set_seek_step(request)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(UserPref.objects.count(), 0)

    def test_playlist_detail_does_not_crash_for_anonymous(self):
        pl = Playlist.objects.create(name="Anon-visible")
        PlaylistItem.objects.create(playlist=pl, video=self.video, order=0)
        request = self._anon_get(reverse("playlist_detail", args=[pl.pk]))
        resp = views.playlist_detail(request, pl.pk)
        self.assertEqual(resp.status_code, 200)

    def test_stream_does_not_crash_for_anonymous(self):
        request = self._anon_get(reverse("stream", args=[self.video.pk]))
        resp = views.stream(request, self.video.pk)
        self.assertEqual(resp.status_code, 200)
