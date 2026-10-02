"""Auth/permission/per-user-state tests.

This app has no test coverage otherwise (see CLAUDE.md), but the
owner/viewer split added on top of django.contrib.auth is exactly the kind
of thing that silently regresses -- a new view added without @owner_required,
or a query that joins the wrong user's PlaybackState row -- so it gets real
tests instead of just manual verification.
"""
import json
import os
import shutil
import sys
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from . import downloader, views
from .auth import LOGIN_ATTEMPT_LIMIT, public_when_enabled
from .models import (
    DownloadJob, DownloadSource, PlaybackState, Playlist, PlaylistItem, SkippedEntry,
    UserPref, Video, WatchEvent,
)

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


class SearchTests(AuthTestCase):
    """views._search_filter -- loose/fuzzy library search."""

    def _titles(self, q):
        qs = Video.objects.filter(missing=False, hidden=False)
        return sorted(views._search_filter(qs, q).values_list("title", flat=True))

    def setUp(self):
        super().setUp()
        self.video.title = "Unrelated"
        self.video.save()
        _make_video(title="Лучшие истории Про ИГРЫ")
        _make_video(title="Candy", filename="r-906 candy.mp4")
        _make_video(title="Other", channel="Sheeno Mirin")
        _make_video(title="Tooboe song")

    def test_non_ascii_search_is_case_insensitive(self):
        self.assertEqual(self._titles("лучшие"), ["Лучшие истории Про ИГРЫ"])
        self.assertEqual(self._titles("игры"), ["Лучшие истории Про ИГРЫ"])

    def test_separators_are_ignored(self):
        self.assertEqual(self._titles("r906"), ["Candy"])
        self.assertEqual(self._titles("r_906"), ["Candy"])

    def test_multiple_words_are_anded_and_channel_is_searched(self):
        self.assertEqual(self._titles("sheeno mirin"), ["Other"])
        self.assertEqual(self._titles("sheeno candy"), [])

    def test_typo_still_matches_but_garbage_does_not(self):
        self.assertEqual(self._titles("toboe"), ["Tooboe song"])
        self.assertEqual(self._titles("zzzqq"), [])

    def test_blank_query_returns_everything(self):
        self.assertEqual(len(self._titles("   ")), 5)


class SeekStepTests(AuthTestCase):
    def _watch_data(self, user):
        self.client.force_login(user)
        resp = self.client.get(reverse("watch_api", args=[self.video.pk]), HTTP_X_SPA="1")
        return resp.json()

    def test_seek_step_absent_until_saved_then_persisted(self):
        # None (not a default 10) lets the client keep its own localStorage
        # value and upload it, instead of the default clobbering it.
        self.assertIsNone(self._watch_data(self.owner)["seek_step"])
        self.client.post(reverse("set_seek_step"), {"value": "30"}, HTTP_X_SPA="1")
        self.assertEqual(self._watch_data(self.owner)["seek_step"], 30)
        # Per-account: another user still has nothing saved.
        self.assertIsNone(self._watch_data(self.viewer)["seek_step"])


class CookieNameTests(TestCase):
    def test_cookies_are_namespaced_so_other_local_apps_cannot_clobber_them(self):
        from django.conf import settings
        self.assertEqual(settings.SESSION_COOKIE_NAME, "homeflix_sessionid")
        self.assertEqual(settings.CSRF_COOKIE_NAME, "homeflix_csrftoken")


# ---- Playlist downloader ----------------------------------------------------
# yt-dlp itself is never run here: fetch/start are patched, and the job runner
# is exercised against a tiny stand-in process that prints yt-dlp-shaped output.

class DownloaderHelperTests(TestCase):
    def test_name_key_ignores_filename_sanitising_differences(self):
        # yt-dlp drops, replaces or full-width-swaps `: ? " | /` depending on
        # version/platform; the playlist title and the file name must still match.
        title = 'Foo: Bar? "Baz" | 日本語 / Qux'
        on_disk = "Foo - Bar Baz ｜ 日本語 ⧸ Qux"
        self.assertEqual(downloader.name_key(title), downloader.name_key(on_disk))
        self.assertEqual(downloader.name_key("ＡＢＣ"), downloader.name_key("abc"))
        self.assertNotEqual(downloader.name_key("Part 1"), downloader.name_key("Part 2"))
        self.assertEqual(downloader.name_key("🎵 !!!"), "")  # nothing comparable -> never matches

    def test_normalize_url(self):
        self.assertEqual(
            downloader.normalize_url("https://www.youtube.com/watch?v=abcdefghijk&list=PL123_-x"),
            "https://www.youtube.com/playlist?list=PL123_-x")
        self.assertEqual(downloader.normalize_url(" https://youtube.com/@chan/videos "),
                         "https://youtube.com/@chan/videos")
        for bad in ["", "youtube.com/playlist?list=PL1", "https://evil.example/playlist?list=PL1",
                    "ftp://youtube.com/x", "javascript:alert(1)"]:
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    downloader.normalize_url(bad)

    def test_normalize_cookies_netscape_with_spaces_and_httponly(self):
        text = ("# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"
                "#HttpOnly_.youtube.com TRUE / TRUE 1999999999 __Secure-3PSID zzz\n")
        body, count, has_login = downloader.normalize_cookies(text)
        self.assertEqual(count, 2)
        self.assertTrue(has_login)
        self.assertIn(".youtube.com\tTRUE\t/\tTRUE\t1999999999\t__Secure-3PSID\tzzz", body)

    def test_normalize_cookies_raw_header(self):
        body, count, has_login = downloader.normalize_cookies(
            "Host: www.youtube.com\ncookie: a=1; b=2=3; LOGIN_INFO=q")
        self.assertEqual(count, 3)
        self.assertTrue(has_login)
        self.assertIn("\tb\t2=3", body)

    def test_normalize_cookies_rejects_garbage(self):
        for bad in ["", "   ", "hello world", "# just a comment"]:
            with self.subTest(text=bad):
                with self.assertRaises(ValueError):
                    downloader.normalize_cookies(bad)

    def test_resolve_target_stays_inside_library(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        with override_settings(LIBRARY_ROOT=root):
            self.assertEqual(downloader.resolve_target(""), os.path.normpath(root))
            self.assertEqual(downloader.resolve_target("a/b"), os.path.join(os.path.normpath(root), "a", "b"))
            for bad in ["..", "../x", "a/../../x", "a\\..\\..\\x"]:
                with self.subTest(sub=bad):
                    with self.assertRaises(ValueError):
                        downloader.resolve_target(bad)
            outside = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, outside, True)
            try:
                os.symlink(outside, os.path.join(root, "link"))
            except (OSError, NotImplementedError):
                return  # no symlink support (Windows without privilege)
            with self.assertRaises(ValueError):
                downloader.resolve_target("link")


class DownloaderCase(AuthTestCase):
    """Per-test scratch library + YTDL_DIR, so nothing here can touch real
    saved cookies or a real library."""

    def setUp(self):
        super().setUp()
        self.lib = tempfile.mkdtemp()
        self.ytdl = tempfile.mkdtemp()
        ov = override_settings(LIBRARY_ROOT=self.lib, YTDL_DIR=self.ytdl)
        ov.enable()
        self.addCleanup(ov.disable)
        self.addCleanup(shutil.rmtree, self.lib, True)
        self.addCleanup(shutil.rmtree, self.ytdl, True)
        self.src = DownloadSource.objects.create(url="https://www.youtube.com/playlist?list=PL1")

    def entries(self, *titles):
        return [{"id": f"vid{i:08d}", "title": t, "duration": 60, "index": i + 1}
                for i, t in enumerate(titles)]

    def cache(self, entries):
        self.src.entries_json = json.dumps(entries)
        self.src.save()


class DownloaderClassifyTests(DownloaderCase):
    def test_statuses(self):
        sub = os.path.join(self.lib, "2026-01", "05")  # Organize puts files in date folders
        os.makedirs(sub)
        open(os.path.join(sub, "Alpha Song.mp4"), "w").close()
        open(os.path.join(self.lib, "notes.txt"), "w").close()   # not a video: ignored
        es = self.entries("Alpha Song", "Beta", "Gamma", "Delta", "[Private video]", "Epsilon")
        # Gamma: only the Lasso archive knows it. Delta: owner-skipped.
        # Epsilon: no file, but a library row's sidecar source URL carries its id.
        archive = downloader.archive_path(self.src)
        with open(archive, "w") as f:
            f.write(f"youtube {es[2]['id']}\n")
        SkippedEntry.objects.create(source=self.src, video_id=es[3]["id"])
        Video.objects.create(
            file_path=os.path.join(self.lib, "renamed.mp4"), rel_path="renamed.mp4",
            filename="renamed.mp4", ext="mp4", title="Totally different",
            source_url=f"https://www.youtube.com/watch?v={es[5]['id']}")
        self.cache(es)
        got = {e["title"]: e["status"] for e in downloader.source_state(self.src)}
        self.assertEqual(got, {
            "Alpha Song": "downloaded", "Beta": "new", "Gamma": "archived",
            "Delta": "skipped", "[Private video]": "unavailable", "Epsilon": "downloaded"})

    def test_subfolder_scopes_the_comparison(self):
        os.makedirs(os.path.join(self.lib, "Music"))
        open(os.path.join(self.lib, "Alpha.mp4"), "w").close()   # outside the target folder
        self.src.target_subdir = "Music"
        self.cache(self.entries("Alpha"))
        self.assertEqual(downloader.source_state(self.src)[0]["status"], "new")

    def test_archive_file_name_matches_lasso(self):
        # md5(url)[:8], same as Lasso's build_cmd(), so the two tools share it.
        import hashlib
        h = hashlib.md5(self.src.url.encode()).hexdigest()[:8]
        self.assertEqual(os.path.basename(downloader.archive_path(self.src)), f"_yt_archive_{h}.txt")


class DownloaderViewTests(DownloaderCase):
    VIEWER_GET = ["downloads", "dl_tools", "dl_job_status"]
    VIEWER_POST = ["dl_cookies", "dl_update_ytdlp", "dl_source_add", "dl_job_cancel"]
    VIEWER_GET_PK = ["dl_source_state"]
    VIEWER_POST_PK = ["dl_source_delete", "dl_source_fetch", "dl_source_start",
                      "dl_source_skip", "dl_source_unskip"]

    def test_viewer_blocked_everywhere(self):
        self.client.force_login(self.viewer)
        for name in self.VIEWER_GET:
            with self.subTest(view=name):
                self.assertEqual(self.client.get(reverse(name), HTTP_X_SPA="1").status_code, 403)
        for name in self.VIEWER_POST:
            with self.subTest(view=name):
                self.assertEqual(self.client.post(reverse(name), {}, HTTP_X_SPA="1").status_code, 403)
        for name in self.VIEWER_GET_PK:
            with self.subTest(view=name):
                self.assertEqual(self.client.get(reverse(name, args=[self.src.pk]), HTTP_X_SPA="1").status_code, 403)
        for name in self.VIEWER_POST_PK:
            with self.subTest(view=name):
                self.assertEqual(self.client.post(reverse(name, args=[self.src.pk]), {}, HTTP_X_SPA="1").status_code, 403)
        self.assertTrue(DownloadSource.objects.filter(pk=self.src.pk).exists())

    def test_anonymous_redirected_to_login(self):
        self.assertEqual(self.client.get(reverse("downloads")).status_code, 302)
        self.assertEqual(self.client.post(reverse("dl_cookies"), {"text": "a=b"}).status_code, 302)
        self.assertFalse(os.path.exists(downloader.cookie_path()))

    def test_owner_page_renders(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("downloads"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "How do I get the cookies?")
        self.assertContains(resp, "Strongly recommended")

    def test_add_source_validates_link_and_folder(self):
        self.client.force_login(self.owner)
        url = reverse("dl_source_add")
        for data in [{"url": "https://evil.example/x"}, {"url": "nonsense"},
                     {"url": "https://www.youtube.com/playlist?list=PL2", "subdir": "../escape"}]:
            with self.subTest(data=data):
                self.assertEqual(self.client.post(url, data, HTTP_X_SPA="1").status_code, 400)
        r = self.client.post(url, {"url": "https://www.youtube.com/watch?v=abcdefghijk&list=PL2",
                                   "subdir": "Music"}, HTTP_X_SPA="1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(DownloadSource.objects.get(url="https://www.youtube.com/playlist?list=PL2").target_subdir, "Music")

    def test_cookies_saved_privately_and_never_echoed(self):
        self.client.force_login(self.owner)
        secret = "S3CR3T-COOKIE-VALUE"
        r = self.client.post(reverse("dl_cookies"), {"text": f"SID={secret}; HSID=other"}, HTTP_X_SPA="1")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["cookies"]["count"], 2)
        for resp in (r, self.client.get(reverse("dl_tools"), HTTP_X_SPA="1"),
                     self.client.get(reverse("downloads"))):
            self.assertNotIn(secret, resp.content.decode())
        path = downloader.cookie_path()
        self.assertIn(secret, open(path).read())
        if os.name != "nt":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        # bad paste: rejected, existing cookies kept
        r = self.client.post(reverse("dl_cookies"), {"text": "hello world"}, HTTP_X_SPA="1")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(os.path.exists(path))
        self.client.post(reverse("dl_cookies"), {"action": "clear"}, HTTP_X_SPA="1")
        self.assertFalse(os.path.exists(path))

    def test_fetch_skip_unskip_flow(self):
        self.client.force_login(self.owner)
        open(os.path.join(self.lib, "Alpha.mp4"), "w").close()
        es = self.entries("Alpha", "Beta")
        with mock.patch.object(downloader, "fetch_playlist", return_value=("My List", es, "")):
            r = self.client.post(reverse("dl_source_fetch", args=[self.src.pk]), {}, HTTP_X_SPA="1")
        data = r.json()
        self.assertEqual(data["source"]["name"], "My List")
        self.assertEqual({e["title"]: e["status"] for e in data["entries"]},
                         {"Alpha": "downloaded", "Beta": "new"})
        beta = es[1]["id"]
        r = self.client.post(reverse("dl_source_skip", args=[self.src.pk]),
                             {"ids": [beta, "not-in-playlist"]}, HTTP_X_SPA="1")
        self.assertEqual(r.json()["counts"].get("skipped"), 1)
        self.assertEqual(SkippedEntry.objects.count(), 1)   # unknown id ignored
        # survives a re-render from cache (no YouTube hit)
        st = self.client.get(reverse("dl_source_state", args=[self.src.pk]), HTTP_X_SPA="1").json()
        self.assertEqual({e["title"]: e["status"] for e in st["entries"]}["Beta"], "skipped")
        r = self.client.post(reverse("dl_source_unskip", args=[self.src.pk]), {"ids": [beta]}, HTTP_X_SPA="1")
        self.assertEqual(r.json()["counts"].get("new"), 1)

    def test_fetch_error_is_reported_and_cache_untouched(self):
        self.client.force_login(self.owner)
        self.cache(self.entries("Keep me"))
        with mock.patch.object(downloader, "fetch_playlist", return_value=("", [], "boom")):
            r = self.client.post(reverse("dl_source_fetch", args=[self.src.pk]), {}, HTTP_X_SPA="1")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["error"], "boom")
        self.src.refresh_from_db()
        self.assertIn("Keep me", self.src.entries_json)

    def test_start_only_passes_ids_from_the_last_fetch(self):
        self.client.force_login(self.owner)
        es = self.entries("Alpha", "Beta")
        self.cache(es)
        seen = {}

        def fake_start(src, ids):
            seen["ids"] = ids
            return DownloadJob.objects.create(source=src, ids=json.dumps(ids), total=len(ids)), ""

        with mock.patch.object(downloader, "start_job", side_effect=fake_start):
            r = self.client.post(reverse("dl_source_start", args=[self.src.pk]),
                                 {"ids": [es[1]["id"], es[1]["id"], "zzzzzzzzzzz", "x; rm -rf /"]},
                                 HTTP_X_SPA="1")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(seen["ids"], [es[1]["id"]])
            r = self.client.post(reverse("dl_source_start", args=[self.src.pk]),
                                 {"ids": ["zzzzzzzzzzz"]}, HTTP_X_SPA="1")
            self.assertEqual(r.status_code, 400)


class DownloaderJobTests(DownloaderCase):
    """_run_job against a stand-in for yt-dlp: prints a progress line, writes
    the before/after marker files for the first id only, and errors the rest."""

    SCRIPT = (
        "import sys\n"
        "mode, before, after, *ids = sys.argv[1:]\n"
        "open(before, 'w').write('\\n'.join(ids) + '\\n')\n"
        "print('HFP|%s|  42.5%%|Some Title' % ids[0])\n"
        "if mode == 'ok':\n"
        "    open(after, 'w').write(ids[0] + '\\n')\n"
        "print(\"ERROR: [youtube] x: Sign in to confirm you're not a bot\")\n"
    )

    def run_job(self, ids, succeed=True):
        job = DownloadJob.objects.create(source=self.src, ids=json.dumps(ids), total=len(ids))

        def fake_build(ids_, out_dir, archive, cookie):
            before = os.path.join(self.ytdl, "b.txt")
            after = os.path.join(self.ytdl, "a.txt")
            return ([sys.executable, "-c", self.SCRIPT, "ok" if succeed else "fail", before, after, *ids_],
                    before, after, os.path.join(self.ytdl, "u.txt"))

        with mock.patch.object(downloader, "build_cmd", side_effect=fake_build), \
             mock.patch.object(downloader.services, "scan_library") as scan:
            downloader._run_job(job.pk)
        job.refresh_from_db()
        return job, scan

    def test_partial_success(self):
        ids = ["aaaaaaaaaaa", "bbbbbbbbbbb"]
        archive = downloader.archive_path(self.src)
        with open(archive, "w") as f:
            f.write("youtube aaaaaaaaaaa\nyoutube ccccccccccc\n")
        job, scan = self.run_job(ids)
        self.assertEqual(job.status, DownloadJob.DONE)
        self.assertEqual((job.done_count, job.fail_count), (1, 1))
        self.assertEqual(job.summary, "1 downloaded, 1 failed or unavailable")
        self.assertIn("not a bot", job.log_tail)
        scan.assert_called_once()
        # the explicitly re-selected id was dropped from the archive; others kept
        self.assertEqual(downloader.read_archive_ids(archive), {"ccccccccccc"})
        # marker files are cleaned up
        self.assertFalse(os.path.exists(os.path.join(self.ytdl, "b.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.ytdl, "a.txt")))

    def test_nothing_downloaded_is_a_failure_with_a_cookie_hint(self):
        job, scan = self.run_job(["aaaaaaaaaaa", "bbbbbbbbbbb"], succeed=False)
        self.assertEqual(job.status, DownloadJob.FAILED)
        self.assertEqual(job.done_count, 0)
        self.assertIn("Paste fresh cookies", job.summary)
        scan.assert_not_called()

    def test_start_job_is_single_flight(self):
        DownloadJob.objects.create(source=self.src, status=DownloadJob.RUNNING, pid=os.getpid())
        with mock.patch.object(downloader, "ytdlp_available", return_value=True):
            job, err = downloader.start_job(self.src, ["aaaaaaaaaaa"])
        self.assertIsNone(job)
        self.assertIn("already running", err)

    def test_dead_pid_job_is_reaped(self):
        j = DownloadJob.objects.create(source=self.src, status=DownloadJob.RUNNING, pid=2_000_000_000)
        downloader.reap_stale()
        j.refresh_from_db()
        self.assertEqual(j.status, DownloadJob.FAILED)

    def test_cancel_marks_job_cancelled(self):
        j = DownloadJob.objects.create(source=self.src, status=DownloadJob.RUNNING)
        self.assertTrue(downloader.cancel_job())
        j.refresh_from_db()
        self.assertEqual(j.status, DownloadJob.CANCELLED)
        self.assertFalse(downloader.cancel_job())

    def test_build_cmd_uses_pasted_cookies_and_shared_archive(self):
        cmd, before, after, batch = downloader.build_cmd(
            ["aaaaaaaaaaa"], self.lib, "/x/_yt_archive_ab.txt", "/x/cookies.txt")
        self.assertEqual(cmd[cmd.index("--cookies") + 1], "/x/cookies.txt")
        self.assertNotIn("--cookies-from-browser", cmd)
        self.assertIn("youtube:player_client=web_embedded,default", cmd)
        self.assertEqual(cmd[cmd.index("--download-archive") + 1], "/x/_yt_archive_ab.txt")
        self.assertNotIn("--playlist-items", cmd)
        self.assertIn("watch?v=aaaaaaaaaaa", open(batch).read())
        # marker/batch files live in YTDL_DIR, not in the library
        for p in (before, after, batch):
            self.assertEqual(os.path.dirname(p), self.ytdl)
        cmd2, *_ = downloader.build_cmd(["aaaaaaaaaaa"], self.lib, "/x/a.txt", None)
        self.assertIn("youtube:player_client=default,-android_sdkless", cmd2)
        self.assertNotIn("--cookies", cmd2)
