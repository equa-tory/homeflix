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
import sqlite3
import sys
import tarfile
import tempfile
import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.db import connection
from datetime import timedelta

from django.utils import timezone
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from . import backup, downloader, refresh, services, views
from .auth import LOGIN_ATTEMPT_LIMIT, public_when_enabled
from .models import (
    DownloadJob, DownloadSource, PlaybackState, Playlist, PlaylistItem, SkippedEntry,
    Setting, SmartPlaylist, Tag, UserPref, Video, WatchEvent,
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
        # Must be >= 3 min: shorter videos never keep a resume point (see NoResumeTests).
        long_video = _make_video(duration_seconds=600.0, title="Long")
        self.client.force_login(self.owner)
        self.client.post(reverse("save_progress", args=[long_video.pk]), {"position": "570"})
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


REAL_CHECK = downloader.check_cookies       # DownloaderCase patches the module attribute


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
        # The live "do the cookies still work" probe talks to YouTube: never in tests
        # (individual tests patch it again to exercise the outcomes).
        p = mock.patch.object(downloader, "check_cookies",
                              return_value={"ok": True, "msg": "Signed in", "checked": time.time()})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(downloader, "check_cookies_async")
        p.start()
        self.addCleanup(p.stop)

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
        self.assertContains(resp, "Required for downloads")

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
        downloader.save_cookies("SID=x; HSID=y")     # jobs refuse to run without cookies
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

    def save_cookies(self):
        downloader.save_cookies("SID=x; HSID=y")

    GOOD_RT = {"name": "deno", "path": "/x/deno", "version": "2.9.7", "supported": True,
               "args": ["--js-runtimes", "deno:/x/deno"]}

    def test_start_job_is_single_flight(self):
        self.save_cookies()
        DownloadJob.objects.create(source=self.src, status=DownloadJob.RUNNING, pid=os.getpid())
        with mock.patch.object(downloader, "ytdlp_available", return_value=True), \
             mock.patch.object(downloader, "js_runtime", return_value=self.GOOD_RT):
            job, err = downloader.start_job(self.src, ["aaaaaaaaaaa"])
        self.assertIsNone(job)
        self.assertIn("already running", err)

    def test_start_job_refuses_without_cookies(self):
        with mock.patch.object(downloader, "ytdlp_available", return_value=True), \
             mock.patch.object(downloader, "js_runtime", return_value=self.GOOD_RT):
            job, err = downloader.start_job(self.src, ["aaaaaaaaaaa"])
        self.assertIsNone(job)
        self.assertIn("cookies are required", err)
        self.assertEqual(DownloadJob.objects.count(), 0)

    def test_start_job_refuses_when_js_runtime_too_old(self):
        # The real-world failure: distro Node 18 is silently rejected by yt-dlp,
        # every video then dies at the challenge step. Say so up front instead.
        self.save_cookies()
        old = {"name": "node", "path": "/usr/bin/node", "version": "18.19.1",
               "supported": False, "args": []}
        with mock.patch.object(downloader, "ytdlp_available", return_value=True), \
             mock.patch.object(downloader, "js_runtime", return_value=old):
            job, err = downloader.start_job(self.src, ["aaaaaaaaaaa"])
        self.assertIsNone(job)
        self.assertIn("Node 18.19.1 is too old", err)
        self.assertEqual(DownloadJob.objects.count(), 0)

    def test_start_job_refuses_when_disk_nearly_full(self):
        self.save_cookies()
        with mock.patch.object(downloader, "ytdlp_available", return_value=True), \
             mock.patch.object(downloader, "js_runtime", return_value=self.GOOD_RT), \
             mock.patch.object(downloader, "free_space", return_value=(200 * 1024 ** 2, 10 * 1024 ** 3)):
            job, err = downloader.start_job(self.src, ["aaaaaaaaaaa"])
        self.assertIsNone(job)
        self.assertIn("200 MB free", err)

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
        with mock.patch.object(downloader, "js_runtime", return_value=self.GOOD_RT):
            cmd, before, after, batch = downloader.build_cmd(
                ["aaaaaaaaaaa"], self.lib, "/x/_yt_archive_ab.txt", "/x/cookies.txt")
        self.assertEqual(cmd[cmd.index("--cookies") + 1], "/x/cookies.txt")
        self.assertNotIn("--cookies-from-browser", cmd)
        self.assertIn("youtube:player_client=web_embedded,default", cmd)
        # the runtime is passed explicitly, so the service needs no PATH entry for it
        self.assertEqual(cmd[cmd.index("--js-runtimes") + 1], "deno:/x/deno")
        self.assertEqual(cmd[cmd.index("--download-archive") + 1], "/x/_yt_archive_ab.txt")
        self.assertNotIn("--playlist-items", cmd)
        self.assertIn("watch?v=aaaaaaaaaaa", open(batch).read())
        # marker/batch files live in YTDL_DIR, not in the library
        for p in (before, after, batch):
            self.assertEqual(os.path.dirname(p), self.ytdl)


class JsRuntimeTests(DownloaderCase):
    """js_runtime() picks a runtime new enough for yt-dlp (it silently ignores
    older ones -- e.g. Node 18 -- and then every download fails)."""

    def pick(self, which, versions, local=None):
        local = local or os.path.join(self.ytdl, "no-such-deno")
        with mock.patch.object(downloader.shutil, "which", side_effect=lambda n: which.get(n)), \
             mock.patch.object(downloader, "local_deno_path", return_value=local), \
             mock.patch.object(downloader, "_exe_version", side_effect=lambda p: versions.get(p)):
            rt = downloader.js_runtime()
            return rt, downloader.runtime_problem(rt)

    def test_old_node_only_is_reported_not_used(self):
        rt, problem = self.pick({"node": "/usr/bin/node"}, {"/usr/bin/node": (18, 19, 1)})
        self.assertFalse(rt["supported"])
        self.assertEqual((rt["name"], rt["version"], rt["args"]), ("node", "18.19.1", []))
        self.assertIn("Node 18.19.1 is too old", problem)
        self.assertIn("install.sh", problem)

    def test_project_local_deno_beats_old_node(self):
        local = os.path.join(self.ytdl, "deno")
        open(local, "w").close()
        rt, problem = self.pick({"node": "/usr/bin/node"},
                                {"/usr/bin/node": (18, 19, 1), local: (2, 9, 7)}, local=local)
        self.assertTrue(rt["supported"])
        self.assertEqual(rt["args"], ["--js-runtimes", f"deno:{local}"])
        self.assertEqual(problem, "")

    def test_skips_too_old_deno_for_new_enough_node(self):
        rt, _ = self.pick({"deno": "/d/deno", "node": "/n/node"},
                          {"/d/deno": (2, 2, 9), "/n/node": (22, 1, 0)})
        self.assertEqual((rt["name"], rt["supported"]), ("node", True))

    def test_nothing_installed(self):
        rt, problem = self.pick({}, {})
        self.assertFalse(rt["supported"])
        self.assertEqual(rt["name"], "")
        self.assertIn("No JavaScript runtime found", problem)

    def test_challenge_failure_hint_names_the_real_problem(self):
        with mock.patch.object(downloader, "js_runtime", return_value={
                "name": "node", "path": "/n", "version": "18.19.1", "supported": False, "args": []}):
            hint = downloader.hint_for("WARNING: n challenge solving failed\nERROR: The page needs to be reloaded.")
        self.assertIn("Node 18.19.1 is too old", hint)


class DiskSpaceTests(DownloaderCase):
    def test_free_space_of_a_folder_that_does_not_exist_yet(self):
        free, total = downloader.free_space(os.path.join(self.lib, "not", "created", "yet"))
        self.assertGreater(total, 0)
        self.assertGreater(free, 0)

    def test_rate_defaults_until_the_library_has_a_few_videos(self):
        self.assertEqual(downloader.estimate_rate(), (downloader.DEFAULT_RATE, "default"))
        Video.objects.all().delete()
        for i in range(3):
            Video.objects.create(
                file_path=os.path.join(self.lib, f"v{i}.mp4"), rel_path=f"v{i}.mp4",
                filename=f"v{i}.mp4", ext="mp4", title=f"v{i}",
                duration_seconds=100, size_bytes=50_000_000)   # 500 KB/s each
        self.assertEqual(downloader.estimate_rate(), (500_000, "library"))

    def test_rate_is_clamped_to_sane_bounds(self):
        Video.objects.all().delete()
        for i in range(3):
            Video.objects.create(
                file_path=os.path.join(self.lib, f"v{i}.mp4"), rel_path=f"v{i}.mp4",
                filename=f"v{i}.mp4", ext="mp4", title=f"v{i}",
                duration_seconds=1, size_bytes=900_000_000)    # absurd 900 MB/s (bad probe data)
        self.assertEqual(downloader.estimate_rate()[0], 4_000_000)

    def test_state_payload_carries_disk_info_and_tools_carry_runtime(self):
        self.client.force_login(self.owner)
        st = self.client.get(reverse("dl_source_state", args=[self.src.pk]), HTTP_X_SPA="1").json()
        self.assertEqual(set(st["disk"]), {"free", "total", "rate", "rate_source"})
        tools = self.client.get(reverse("dl_tools"), HTTP_X_SPA="1").json()
        self.assertEqual(set(tools["runtime"]), {"name", "version", "supported", "problem"})


# ---- Streaming: bounded ranges (the "video randomly stops" fix) --------------

class StreamRangeTests(AuthTestCase):
    """An open-ended `Range: bytes=N-` used to be answered with the whole rest
    of the file in ONE response, which pinned a gunicorn worker until the
    player had consumed it -- and gunicorn SIGKILLs a sync worker past
    --timeout, cutting playback off mid-video. Open-ended ranges are now
    capped; explicit ones are honoured."""

    SIZE = 10_000

    def setUp(self):
        super().setUp()
        self.data = bytes(i % 251 for i in range(self.SIZE))
        with open(self.video.file_path, "wb") as f:
            f.write(self.data)
        self.client.force_login(self.owner)
        cap = mock.patch.object(views, "MAX_RANGE_BYTES", 1000)
        cap.start()
        self.addCleanup(cap.stop)

    def get(self, rng):
        resp = self.client.get(reverse("stream", args=[self.video.pk]), HTTP_RANGE=rng)
        body = b"".join(resp.streaming_content) if resp.streaming else resp.content
        return resp, body

    def test_open_ended_range_is_capped(self):
        resp, body = self.get("bytes=0-")
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp["Content-Range"], f"bytes 0-999/{self.SIZE}")
        self.assertEqual(resp["Content-Length"], "1000")
        self.assertEqual(body, self.data[:1000])

    def test_open_ended_range_mid_file(self):
        resp, body = self.get("bytes=500-")
        self.assertEqual(resp["Content-Range"], f"bytes 500-1499/{self.SIZE}")
        self.assertEqual(body, self.data[500:1500])

    def test_open_ended_range_near_the_end_is_not_overrun(self):
        resp, body = self.get(f"bytes={self.SIZE - 10}-")
        self.assertEqual(resp["Content-Range"], f"bytes {self.SIZE - 10}-{self.SIZE - 1}/{self.SIZE}")
        self.assertEqual(body, self.data[-10:])

    def test_explicit_end_is_honoured_even_above_the_cap(self):
        resp, body = self.get("bytes=0-1999")
        self.assertEqual(resp["Content-Length"], "2000")
        self.assertEqual(body, self.data[:2000])

    def test_range_past_the_end_is_416(self):
        resp, _ = self.get(f"bytes={self.SIZE}-")
        self.assertEqual(resp.status_code, 416)
        self.assertEqual(resp["Content-Range"], f"bytes */{self.SIZE}")

    def test_no_range_header_still_serves_the_whole_file(self):
        resp = self.client.get(reverse("stream", args=[self.video.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b"".join(resp.streaming_content), self.data)
        self.assertEqual(resp["Accept-Ranges"], "bytes")


# ---- Shorts and < 3 min videos never resume -----------------------------------

class NoResumeTests(AuthTestCase):
    def mk(self, name, w, h, dur):
        return _make_video(title=name, width=w, height=h, duration_seconds=dur)

    def setUp(self):
        super().setUp()
        self.long = self.mk("long", 1920, 1080, 600)
        self.short = self.mk("short", 1920, 1080, 120)        # < 3 min
        self.portrait = self.mk("portrait", 720, 1280, 600)   # a "short"
        self.square = self.mk("square", 1080, 1080, 600)      # counts as vertical
        self.unknown = self.mk("unknown", 1920, 1080, None)   # unknown length: normal
        self.edge = self.mk("edge", 1920, 1080, 180)          # exactly 3:00 is NOT shorter
        self.client.force_login(self.owner)

    def state(self, video, pos):
        return PlaybackState.objects.create(video=video, user=self.owner, position_seconds=pos)

    def test_remembers_position_rules(self):
        got = {v.title: v.remembers_position for v in
               (self.long, self.short, self.portrait, self.square, self.unknown, self.edge)}
        self.assertEqual(got, {"long": True, "short": False, "portrait": False,
                               "square": False, "unknown": True, "edge": True})

    def test_save_progress_ignored_for_short_videos(self):
        for v in (self.short, self.portrait):
            r = self.client.post(reverse("save_progress", args=[v.pk]), {"position": "50"})
            self.assertEqual(r.status_code, 200)
            self.assertFalse(PlaybackState.objects.filter(video=v).exists(), v.title)
        self.client.post(reverse("save_progress", args=[self.long.pk]), {"position": "50"})
        self.assertEqual(PlaybackState.objects.get(video=self.long).position_seconds, 50)

    def test_watch_data_starts_short_videos_from_zero_even_with_an_old_position(self):
        self.state(self.short, 90)
        self.state(self.portrait, 300)
        self.state(self.long, 300)
        for v, pos, remember in ((self.short, 0, False), (self.portrait, 0, False), (self.long, 300, True)):
            with self.subTest(video=v.title):
                d = self.client.get(reverse("watch_api", args=[v.pk])).json()
                self.assertEqual((d["position"], d["remember_position"]), (pos, remember))

    def test_continue_watching_excludes_them(self):
        for v in (self.long, self.short, self.portrait, self.square, self.unknown):
            self.state(v, 100)
        resp = self.client.get(reverse("home"))
        titles = {v.title for v in resp.context["continue_watching"]}
        self.assertEqual(titles, {"long", "unknown"})

    def test_no_progress_bar_for_them(self):
        from .templatetags.library_extras import pct
        long_state, short_state = self.state(self.long, 300), self.state(self.short, 60)
        self.assertEqual(pct(long_state, self.long), 50)
        self.assertEqual(pct(short_state, self.short), 0)
        self.short.my_playback, self.long.my_playback = [short_state], [long_state]
        self.assertEqual(views._serialize(self.short)["progress"], 0)
        self.assertEqual(views._serialize(self.long)["progress"], 50)


# ---- Dice button -----------------------------------------------------------------

class DiceButtonTests(AuthTestCase):
    def test_dice_and_shared_random_helper_are_in_the_shell(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("library")).content.decode()
        self.assertIn('id="diceFab"', html)
        self.assertIn("function openRandom(", html)
        self.assertIn("function randomScope(", html)


# ---- Scanner: yt-dlp temp files and races ----------------------------------------------

class ScannerHardeningTests(AuthTestCase):
    def test_ytdlp_intermediates_are_recognised(self):
        for name in ("Song.f401.mp4", "Song.f251-9.webm", "Song.temp.mp4"):
            self.assertTrue(services.is_ytdlp_temp(name), name)
        for name in ("Normal Title.mp4", "A.F1.fan.mp4", "Show.2024.mp4", "x.format.mp4"):
            self.assertFalse(services.is_ytdlp_temp(name), name)

    def test_scan_skips_downloads_in_progress(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        for n in ("Real.mp4", "Real.f401.mp4", "Real.temp.mp4"):
            open(os.path.join(root, n), "w").close()
        services.scan_library(root=root, make_thumbs=False)
        self.assertEqual(sorted(Video.objects.filter(file_path__startswith=root).values_list("filename", flat=True)),
                         ["Real.mp4"])

    def test_scan_survives_a_vanished_file_and_a_concurrent_insert(self):
        from django.db import IntegrityError
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        for n in ("a.mp4", "b.mp4", "c.mp4"):
            open(os.path.join(root, n), "w").close()
        real = services._scan_file

        def flaky(root_, full, name, ext, make_thumbs):
            if name == "a.mp4":
                raise FileNotFoundError(full)
            if name == "b.mp4":
                raise IntegrityError("UNIQUE constraint failed: library_video.file_path")
            return real(root_, full, name, ext, make_thumbs)

        with mock.patch.object(services, "_scan_file", side_effect=flaky):
            summary = services.scan_library(root=root, make_thumbs=False)
        self.assertEqual(summary["added"], 1)           # only c.mp4 got through; no exception


# ---- Backups -------------------------------------------------------------------------------

class BackupDirs:
    """Scratch backup/thumbnail/subtitle folders (mixin for the test cases)."""

    def make_dirs(self):
        self.bk = tempfile.mkdtemp()
        self.thumbs = tempfile.mkdtemp()
        self.subs = tempfile.mkdtemp()
        for d in (self.bk, self.thumbs, self.subs):
            self.addCleanup(shutil.rmtree, d, True)
        ov = override_settings(THUMBNAIL_DIR=self.thumbs, SUBTITLE_DIR=self.subs)
        ov.enable()
        self.addCleanup(ov.disable)
        open(os.path.join(self.thumbs, "video_1.jpg"), "wb").write(b"jpg")
        os.makedirs(os.path.join(self.thumbs, "frames"))
        open(os.path.join(self.thumbs, "frames", "f.jpg"), "wb").write(b"regenerable")
        open(os.path.join(self.subs, "x.vtt"), "w").write("WEBVTT")
        Setting.set("backup_path", self.bk)


class BackupCase(BackupDirs, AuthTestCase):
    def setUp(self):
        super().setUp()
        self.make_dirs()


class BackupRunTests(BackupDirs, TransactionTestCase):
    """TransactionTestCase on purpose: SQLite's online-backup API can't read a
    database through a connection that is inside an open transaction (what a
    plain TestCase wraps every test in) -- it just waits for a lock that never
    clears. The app runs it from a thread in autocommit mode, like this."""

    def setUp(self):
        User.objects.create_user("owner", password="pw-owner-1234", is_staff=True)
        User.objects.create_user("viewer", password="pw-viewer-1234")
        self.make_dirs()

    def test_archive_contents_and_database_snapshot(self):
        res = backup.run_backup()
        self.assertTrue(res["ok"], res)
        files = backup.list_backups(self.bk)
        self.assertEqual(len(files), 1)
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        with tarfile.open(os.path.join(self.bk, files[0]["name"])) as tar:
            names = tar.getnames()
            tar.extractall(out)
        self.assertIn("db.sqlite3", names)
        self.assertIn("thumbnails/video_1.jpg", names)
        self.assertIn("subtitles/x.vtt", names)
        self.assertIn("MANIFEST.json", names)
        self.assertNotIn("thumbnails/frames/f.jpg", names)       # regenerable: left out
        con = sqlite3.connect(os.path.join(out, "db.sqlite3"))
        try:
            users = {r[0] for r in con.execute("select username from auth_user")}
        finally:
            con.close()
        self.assertTrue({"owner", "viewer"} <= users)            # a real, readable snapshot
        self.assertEqual([f for f in os.listdir(self.bk) if f.startswith(".")], [])   # temp files cleaned
        self.assertTrue(Setting.get("backup_last_ok"))
        self.assertEqual(Setting.get("backup_running"), "")

    def test_lock_watch_gives_up_only_after_continuous_blocking(self):
        clock = [1000.0]
        with mock.patch.object(backup.time, "time", side_effect=lambda: clock[0]):
            watch = backup.lock_watch(10)
            watch(5, 0, 0)                      # busy: starts the clock
            clock[0] += 9
            watch(6, 0, 0)                      # locked, 9 s in: still waiting
            clock[0] += 5
            watch(0, 4, 8)                      # progress made: the clock resets
            clock[0] += 9
            watch(5, 4, 8)
            clock[0] += 9
            watch(5, 4, 8)                      # 9 s into a new block: fine
            clock[0] += 2
            with self.assertRaises(TimeoutError):
                watch(5, 4, 8)                  # 11 s continuously blocked

    def test_retention_only_touches_our_own_files(self):
        Setting.set("backup_keep", 2)
        for stamp in ("20260101-000000", "20260102-000000", "20260103-000000", "20260104-000000"):
            open(os.path.join(self.bk, f"homeflix-{stamp}.tar.gz"), "wb").write(b"old")
        for other in ("notes.txt", "homeflix-backup.tar.gz", "homeflix-20260101.tar.gz", "photos.tar.gz"):
            open(os.path.join(self.bk, other), "w").write("mine")
        self.assertTrue(backup.run_backup()["ok"])
        ours = [f["name"] for f in backup.list_backups(self.bk)]
        self.assertEqual(len(ours), 2)
        self.assertIn("homeflix-20260104-000000.tar.gz", ours)    # newest old one kept
        self.assertNotIn("homeflix-20260101-000000.tar.gz", ours)
        for other in ("notes.txt", "homeflix-backup.tar.gz", "homeflix-20260101.tar.gz", "photos.tar.gz"):
            self.assertTrue(os.path.exists(os.path.join(self.bk, other)), other)

    def test_failed_backup_deletes_nothing(self):
        Setting.set("backup_keep", 1)
        old = os.path.join(self.bk, "homeflix-20260101-000000.tar.gz")
        open(old, "wb").write(b"precious")
        with mock.patch.object(backup, "_free", return_value=1):    # pretend the disk is full
            res = backup.run_backup()
        self.assertFalse(res["ok"])
        self.assertIn("Not enough free space", res["msg"])
        self.assertTrue(os.path.exists(old))
        self.assertEqual(os.listdir(self.bk), ["homeflix-20260101-000000.tar.gz"])
        self.assertEqual(Setting.get("backup_running"), "")


class BackupConfigTests(BackupCase):
    def test_path_validation(self):
        for bad in ("", "relative/dir", "/", os.path.join(self.thumbs, "sub"), self.subs):
            with self.subTest(path=bad):
                with self.assertRaises(ValueError):
                    backup.validate_path(bad)
        self.assertEqual(backup.validate_path(self.bk + "/"), os.path.normpath(self.bk))

    def test_save_config_bounds_and_unwritable_path(self):
        for kwargs in (dict(hours="0"), dict(hours="abc"), dict(keep="0"), dict(keep="999"),
                       dict(path="/proc/definitely/not/writable")):
            args = dict(enabled=True, hours="48", path=self.bk, keep="1")
            args.update(kwargs)
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    backup.save_config(**args)
        backup.save_config(True, "12", self.bk, "3")
        self.assertEqual(backup.get_config(), {"enabled": True, "hours": 12, "path": self.bk, "keep": 3})

    def test_defaults_are_the_requested_ones(self):
        Setting.objects.filter(key__startswith="backup_").delete()
        self.assertEqual(backup.get_config(), {"enabled": True, "hours": 48,
                                               "path": "/mnt/ssd/backups/homeflix", "keep": 1})


class BackupSchedulingTests(BackupCase):
    def test_claim_lets_exactly_one_caller_win_per_interval(self):
        self.assertTrue(backup.claim("t", 100))
        self.assertFalse(backup.claim("t", 100))                  # the other "workers"
        self.assertFalse(backup.claim("t", 100))
        Setting.set("claim_t", repr(time.time() - 101))
        self.assertTrue(backup.claim("t", 100))                   # interval elapsed

    def test_claim_is_a_compare_and_swap(self):
        # Simulate the loser of a race: it read the old value, someone else
        # swapped it first -> its conditional update must match nothing.
        backup.claim("race", 0)
        stale = Setting.get("claim_race")
        Setting.set("claim_race", repr(time.time() + 5))
        self.assertEqual(Setting.objects.filter(key="claim_race", value=stale).update(value="x"), 0)

    def test_maybe_run_respects_enabled_and_due(self):
        with mock.patch.object(backup, "run_backup", return_value={"ok": True}) as run:
            Setting.set("backup_enabled", "0")
            self.assertIsNone(backup.maybe_run())
            Setting.set("backup_enabled", "1")
            Setting.set("backup_last_ok", repr(time.time() - 3600))          # 1 h ago, interval 48 h
            self.assertIsNone(backup.maybe_run())
            Setting.set("backup_last_ok", repr(time.time() - 49 * 3600))     # overdue
            self.assertEqual(backup.maybe_run(), {"ok": True})
            self.assertIsNone(backup.maybe_run())                            # claim blocks a second worker
            self.assertEqual(run.call_count, 1)

    def test_status_reports_next_due(self):
        Setting.set("backup_last_ok", repr(1_000_000.0))
        Setting.set("backup_hours", 10)
        self.assertEqual(backup.status()["next_due"], 1_000_000.0 + 36000)


class BackupViewTests(BackupCase):
    def test_viewer_blocked(self):
        self.client.force_login(self.viewer)
        for name in ("backups", "backups_status"):
            self.assertEqual(self.client.get(reverse(name), HTTP_X_SPA="1").status_code, 403, name)
        for name in ("backups_save", "backups_run"):
            self.assertEqual(self.client.post(reverse(name), {}, HTTP_X_SPA="1").status_code, 403, name)

    def test_owner_page_save_and_run(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("backups")), "Back up now")
        r = self.client.post(reverse("backups_save"), {"enabled": "1", "hours": "24", "path": self.bk, "keep": "2"},
                             HTTP_X_SPA="1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["hours"], r.json()["keep"]), (24, 2))
        r = self.client.post(reverse("backups_save"), {"enabled": "1", "hours": "24", "path": "relative", "keep": "2"},
                             HTTP_X_SPA="1")
        self.assertEqual(r.status_code, 400)
        with mock.patch.object(backup.threading, "Thread") as th:        # don't actually spawn
            r = self.client.post(reverse("backups_run"), {}, HTTP_X_SPA="1")
        self.assertEqual(r.status_code, 200)
        th.assert_called_once()
        self.assertTrue(r.json()["running"])
        r = self.client.post(reverse("backups_run"), {}, HTTP_X_SPA="1")  # already "running"
        self.assertEqual(r.status_code, 409)


# ---- Refresh library data from YouTube ---------------------------------------------------------

class RefreshCase(DownloaderCase):
    def entry(self, i, title, channel="Chan"):
        return {"id": f"vid{i:08d}", "title": title, "duration": 100, "index": i, "channel": channel}

    def mkvideo(self, title, **kw):
        defaults = dict(file_path=os.path.join(self.lib, f"{title}.mp4"), rel_path=f"{title}.mp4",
                        filename=f"{title}.mp4", ext="mp4", title=title, width=1920, height=1080,
                        duration_seconds=100)
        defaults.update(kw)
        return Video.objects.create(**defaults)


class RefreshMatchTests(RefreshCase):
    def test_matching_by_id_title_and_filename_and_ambiguity(self):
        entries = refresh.merge_entries([[self.entry(1, "Alpha: Song?"), self.entry(2, "Dup"), self.entry(3, "Dup")],
                                         [self.entry(4, "Beta", channel="")]])
        by_id, by_name = refresh.build_index(entries)
        v_title = self.mkvideo("Alpha - Song")                                   # sanitised title
        v_file = self.mkvideo("zzz", filename="Beta.mp4")                        # only the file name matches
        v_id = self.mkvideo("Totally different",
                          source_url="https://www.youtube.com/watch?v=vid00000004")
        v_dup = self.mkvideo("Dup")
        v_none = self.mkvideo("Not in any playlist")
        self.assertEqual(refresh.match_video(v_title, by_id, by_name)["id"], "vid00000001")
        self.assertEqual(refresh.match_video(v_file, by_id, by_name)["id"], "vid00000004")
        self.assertEqual(refresh.match_video(v_id, by_id, by_name)["id"], "vid00000004")
        self.assertIsNone(refresh.match_video(v_dup, by_id, by_name))          # ambiguous: never guessed
        self.assertIsNone(refresh.match_video(v_none, by_id, by_name))

    def test_merge_prefers_the_entry_that_has_a_channel(self):
        merged = refresh.merge_entries([[self.entry(1, "A", channel="")], [self.entry(1, "A", channel="Real")]])
        self.assertEqual(merged[0]["channel"], "Real")


class RefreshApplyTests(RefreshCase):
    def test_fields_are_opt_in_and_portrait_keeps_its_thumbnail(self):
        e = self.entry(1, "YouTube Title", channel="The Channel")
        v = self.mkvideo("file name", channel="old")
        with mock.patch.object(refresh, "fetch_thumbnail", return_value=True) as ft:
            changed = refresh.apply_entry(v, e, {"channel": True})              # only the author
        ft.assert_not_called()
        v.refresh_from_db()
        self.assertEqual((v.channel, v.title), ("The Channel", "file name"))
        self.assertEqual(v.source_url, "https://www.youtube.com/watch?v=vid00000001")
        self.assertEqual(changed, {"channel", "url"})

        with mock.patch.object(refresh, "fetch_thumbnail", return_value=True):
            changed = refresh.apply_entry(v, e, {"title": True, "thumbs": True})
        v.refresh_from_db()
        self.assertEqual(v.title, "YouTube Title")
        self.assertEqual(v.thumbnail_path, os.path.join(self.thumbs_dir(), f"video_{v.pk}.jpg"))
        self.assertEqual(changed, {"title", "thumb"})

        shorts = self.mkvideo("vertical", width=720, height=1280)
        with mock.patch.object(refresh, "fetch_thumbnail", return_value=True) as ft:
            refresh.apply_entry(shorts, e, {"thumbs": True})
        ft.assert_not_called()

    def thumbs_dir(self):
        from django.conf import settings
        return settings.THUMBNAIL_DIR

    def test_failed_thumbnail_download_is_not_an_error(self):
        v = self.mkvideo("x")
        with mock.patch.object(refresh, "fetch_thumbnail", return_value=False):
            changed = refresh.apply_entry(v, self.entry(1, "x"), {"thumbs": True})
        self.assertNotIn("thumb", changed)
        self.assertEqual(v.thumbnail_path, "")

    def test_thumbnail_tries_candidates_best_first_and_leaves_dest_alone_on_failure(self):
        dest = os.path.join(self.lib, "t.jpg")
        urls = []

        def fake_get(url):
            urls.append(url.rsplit("/", 1)[1])
            return None if "maxres" in url else b"x" * 5000

        def fake_ffmpeg(src, out):
            open(out, "wb").write(b"scaled")
            return 0, b"", b""

        with mock.patch.object(refresh, "_http_get", side_effect=fake_get), \
             mock.patch.object(refresh, "_run_ffmpeg", side_effect=fake_ffmpeg):
            self.assertTrue(refresh.fetch_thumbnail("vid00000001", dest))
        self.assertEqual(urls, ["maxresdefault.jpg", "hq720.jpg"])
        self.assertEqual(open(dest, "rb").read(), b"scaled")
        with mock.patch.object(refresh, "_http_get", return_value=None):
            self.assertFalse(refresh.fetch_thumbnail("vid00000002", dest))
        self.assertEqual(open(dest, "rb").read(), b"scaled")                    # untouched


class RefreshJobTests(RefreshCase):
    def test_full_run_updates_matches_and_reports(self):
        self.src.name = "mine"
        self.src.save()
        v1 = self.mkvideo("Alpha")
        v2 = self.mkvideo("Beta", channel="keep?")
        v3 = self.mkvideo("Unlisted")
        es = [self.entry(1, "Alpha", "Chan A"), self.entry(2, "Beta", "Chan B")]
        with mock.patch.object(downloader, "fetch_playlist", return_value=("Fresh Name", es, "")), \
             mock.patch.object(refresh, "fetch_thumbnail", return_value=True):
            refresh._run({"thumbs": True, "channel": True, "title": False})
        st = refresh.status()
        self.assertEqual(st["state"], "done", st)
        self.assertEqual((st["total"], st["matched"], st["changed"], st["thumbs"]), (Video.objects.filter(missing=False).count(), 2, 2, 2))
        self.assertIn("Matched 2 of", st["msg"])
        for v, ch in ((v1, "Chan A"), (v2, "Chan B"), (v3, "")):
            v.refresh_from_db()
            self.assertEqual(v.channel, ch)
        self.src.refresh_from_db()
        self.assertEqual(self.src.name, "Fresh Name")
        self.assertEqual(json.loads(self.src.entries_json)[0]["channel"], "Chan A")   # cache now carries authors

    def test_falls_back_to_the_saved_list_when_youtube_is_unreachable(self):
        self.cache([self.entry(1, "Alpha", "Cached Chan")])
        v = self.mkvideo("Alpha")
        with mock.patch.object(downloader, "fetch_playlist", return_value=("", [], "no network")):
            refresh._run({"channel": True})
        v.refresh_from_db()
        self.assertEqual(v.channel, "Cached Chan")
        self.assertIn("used the saved list", refresh.status()["msg"])

    def test_no_saved_playlists(self):
        DownloadSource.objects.all().delete()
        refresh._run({"channel": True})
        self.assertEqual(refresh.status()["state"], "error")

    def test_one_bad_video_does_not_stop_the_run(self):
        self.mkvideo("Alpha"); self.mkvideo("Beta")
        es = [self.entry(1, "Alpha"), self.entry(2, "Beta")]
        calls = []

        def flaky(video, entry, fields):
            calls.append(video.title)
            if video.title == "Alpha":
                raise RuntimeError("boom")
            return {"channel"}

        with mock.patch.object(downloader, "fetch_playlist", return_value=("n", es, "")), \
             mock.patch.object(refresh, "apply_entry", side_effect=flaky):
            refresh._run({"channel": True})
        st = refresh.status()
        self.assertEqual((st["state"], st["failed"], st["changed"]), ("done", 1, 1))

    def test_stale_running_status_reads_as_interrupted(self):
        refresh._write(state="running", phase="x")
        with mock.patch.object(refresh.time, "time", return_value=time.time() + 500):
            self.assertEqual(refresh.status()["state"], "error")


class RefreshViewTests(RefreshCase):
    def test_viewer_blocked_and_owner_validation(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post(reverse("dl_refresh"), {"thumbs": "1"}, HTTP_X_SPA="1").status_code, 403)
        self.assertEqual(self.client.get(reverse("dl_refresh_status"), HTTP_X_SPA="1").status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(reverse("dl_refresh"), {}, HTTP_X_SPA="1").status_code, 400)
        with mock.patch.object(downloader, "ytdlp_available", return_value=True), \
             mock.patch.object(refresh, "start_refresh", return_value=True) as sr:
            r = self.client.post(reverse("dl_refresh"), {"thumbs": "1", "channel": "1"}, HTTP_X_SPA="1")
        self.assertEqual(r.status_code, 200)
        sr.assert_called_once_with({"thumbs": True, "channel": True, "title": False})
        with mock.patch.object(downloader, "ytdlp_available", return_value=True), \
             mock.patch.object(refresh, "start_refresh", return_value=False):
            r = self.client.post(reverse("dl_refresh"), {"thumbs": "1"}, HTTP_X_SPA="1")
        self.assertEqual(r.status_code, 409)

    def test_downloads_page_has_the_card(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("downloads")), "Update library from YouTube")


# ---- Smart playlists: ALL (and) / ANY (or) ----------------------------------------

class SmartMatchModeTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.a = _make_video(title="Alpha one", rating=0)
        self.b = _make_video(title="Beta two", favorite=True)
        self.c = _make_video(title="Alpha fav", favorite=True)
        self.d = _make_video(title="Gamma")
        self.sp = SmartPlaylist.objects.create(name="s")

    def titles(self):
        return sorted(v.title for v in self.sp.get_videos() if v.pk != self.video.pk)

    def rules(self, mode, *rules):
        self.sp.match_mode = mode
        self.sp.rules = json.dumps(list(rules))
        self.sp.save()

    TITLE = {"field": "title", "op": "contains", "value": "alpha"}
    FAV = {"field": "favorite", "op": "is", "value": ""}

    def test_all_requires_every_rule_any_requires_one(self):
        self.rules("all", self.TITLE, self.FAV)
        self.assertEqual(self.titles(), ["Alpha fav"])
        self.rules("any", self.TITLE, self.FAV)
        self.assertEqual(self.titles(), ["Alpha fav", "Alpha one", "Beta two"])

    def test_default_mode_is_all(self):
        self.assertEqual(SmartPlaylist.objects.create(name="n").match_mode, "all")

    def test_incomplete_rules_are_ignored_not_match_everything(self):
        empty = {"field": "title", "op": "contains", "value": "  "}
        unknown = {"field": "nonsense", "op": "is", "value": "x"}
        bad_number = {"field": "rating", "op": "gte", "value": "abc"}
        self.rules("any", self.TITLE, empty, unknown, bad_number)
        self.assertEqual(self.titles(), ["Alpha fav", "Alpha one"])    # not the whole library
        self.rules("all", empty)                                       # nothing valid -> everything, as before
        self.assertEqual(len(self.titles()), 4)

    def test_any_with_not_contains_and_number_rules(self):
        self.a.rating = 5
        self.a.save()
        self.rules("any", {"field": "rating", "op": "gte", "value": "5"},
                   {"field": "title", "op": "contains", "value": "gamma"})
        self.assertEqual(self.titles(), ["Alpha one", "Gamma"])
        self.rules("all", {"field": "title", "op": "not_contains", "value": "alpha"})
        self.assertEqual(self.titles(), ["Beta two", "Gamma"])

    def test_two_tag_rules_all_means_both_tags_any_means_either(self):
        t1, t2 = Tag.objects.create(name="t1"), Tag.objects.create(name="t2")
        self.a.tags.add(t1, t2)
        self.b.tags.add(t1)
        self.c.tags.add(t2)
        r1, r2 = ({"field": "tags", "op": "is", "value": n} for n in ("t1", "t2"))
        self.rules("all", r1, r2)
        self.assertEqual(self.titles(), ["Alpha one"])
        self.rules("any", r1, r2)
        self.assertEqual(self.titles(), ["Alpha fav", "Alpha one", "Beta two"])    # no duplicates

    def test_save_rules_view_stores_match_mode(self):
        self.client.force_login(self.owner)
        url = reverse("save_smart_rules", args=[self.sp.pk])
        self.client.post(url, {"name": "s", "rules": json.dumps([self.TITLE]), "match": "any"})
        self.sp.refresh_from_db()
        self.assertEqual(self.sp.match_mode, "any")
        self.client.post(url, {"name": "s", "rules": "[]", "match": "bogus"})
        self.sp.refresh_from_db()
        self.assertEqual(self.sp.match_mode, "any")                  # junk value ignored
        page = self.client.get(reverse("smart_playlist_detail", args=[self.sp.pk])).content.decode()
        self.assertIn('<option value="any" selected>', page)


# ---- Playlist pages: collapsed settings, remembered sort --------------------------------

class PlaylistPageTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.pl = Playlist.objects.create(name="P")
        self.sp = SmartPlaylist.objects.create(name="S")
        self.other = Playlist.objects.create(name="Other")

    def ctx(self, name, pk, user, **params):
        self.client.force_login(user)
        resp = self.client.get(reverse(name, args=[pk]), params)
        return resp.context["sort"], resp.context["rev"]

    def test_settings_are_collapsed_by_default(self):
        self.client.force_login(self.owner)
        for name, pk in (("playlist_detail", self.pl.pk), ("smart_playlist_detail", self.sp.pk)):
            html = self.client.get(reverse(name, args=[pk])).content.decode()
            self.assertIn('<details class="pl-settings">', html, name)
            self.assertNotIn('<details class="pl-settings" open', html)
            self.assertIn("plThumbTool", html)                      # cover tool is inside it
        self.assertIn("rulesForm", self.client.get(reverse("smart_playlist_detail", args=[self.sp.pk])).content.decode())

    def test_sort_is_remembered_per_playlist_and_per_account(self):
        self.assertEqual(self.ctx("playlist_detail", self.pl.pk, self.owner), ("manual", False))
        self.assertEqual(self.ctx("playlist_detail", self.pl.pk, self.owner, sort="duration", rev="1"), ("duration", True))
        # reopened with no query: remembered
        self.assertEqual(self.ctx("playlist_detail", self.pl.pk, self.owner), ("duration", True))
        # another playlist, another account: unaffected
        self.assertEqual(self.ctx("playlist_detail", self.other.pk, self.owner), ("manual", False))
        self.assertEqual(self.ctx("playlist_detail", self.pl.pk, self.viewer), ("manual", False))
        # an explicit choice replaces it
        self.assertEqual(self.ctx("playlist_detail", self.pl.pk, self.owner, sort="rating"), ("rating", False))
        self.assertEqual(self.ctx("playlist_detail", self.pl.pk, self.owner), ("rating", False))

    def test_unknown_sort_is_not_remembered(self):
        self.ctx("playlist_detail", self.pl.pk, self.owner, sort="duration")
        self.ctx("playlist_detail", self.pl.pk, self.owner, sort="../../etc")
        self.assertEqual(self.ctx("playlist_detail", self.pl.pk, self.owner), ("duration", False))

    def test_smart_playlist_sort_is_remembered_too(self):
        self.assertEqual(self.ctx("smart_playlist_detail", self.sp.pk, self.owner), ("added", False))
        self.ctx("smart_playlist_detail", self.sp.pk, self.owner, sort="duration", rev="1")
        self.assertEqual(self.ctx("smart_playlist_detail", self.sp.pk, self.owner), ("duration", True))
        self.assertEqual(self.ctx("smart_playlist_detail", self.sp.pk, self.viewer), ("added", False))
        self.assertEqual(UserPref.get(self.owner, f"spsort_{self.sp.pk}"), "duration:1")


# ---- Channels tab -------------------------------------------------------------------------------

class ChannelsTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.vids = {}
        spec = [("a1", "Chan A"), ("a2", "Chan A"), ("a3", "Chan A"), ("a4", "Chan A"), ("a5", "Chan A"),
                ("b1", "Beta Ch"), ("z1", "zeta ch"), ("n1", "")]
        for i, (t, ch) in enumerate(spec):
            self.vids[t] = _make_video(title=t, channel=ch, thumbnail_path=f"/t/{t}.jpg")
            Video.objects.filter(pk=self.vids[t].pk).update(
                date_added=timezone.now() - timedelta(hours=len(spec) - i))   # later in list = newer
        _make_video(title="hidden", channel="Chan A", hidden=True, thumbnail_path="/t/h.jpg")
        _make_video(title="gone", channel="Chan A", missing=True, thumbnail_path="/t/g.jpg")
        _make_video(title="nothumb", channel="Beta Ch")
        self.client.force_login(self.owner)

    def page(self, **params):
        return self.client.get(reverse("channels"), params)

    def by_name(self, resp):
        return {c["name"]: c for c in resp.context["channels"]}

    def test_counts_exclude_hidden_missing_and_unnamed(self):
        ch = self.by_name(self.page())
        self.assertEqual({n: c["count"] for n, c in ch.items()}, {"Chan A": 5, "Beta Ch": 2, "zeta ch": 1})
        self.assertEqual(self.page().context["no_channel"], 2)            # n1 + the AuthTestCase default video

    def test_cover_is_up_to_four_newest_thumbnails(self):
        covers = self.by_name(self.page())["Chan A"]["covers"]
        self.assertEqual(len(covers), 4)
        self.assertEqual(covers, [self.vids[t].pk for t in ("a5", "a4", "a3", "a2")])   # newest first
        self.assertEqual(self.by_name(self.page())["Beta Ch"]["covers"], [self.vids["b1"].pk])   # no-thumb video skipped

    def test_sorting(self):
        order = lambda **p: [c["name"] for c in self.page(**p).context["channels"]]
        self.assertEqual(order(), ["Chan A", "Beta Ch", "zeta ch"])                      # most videos
        self.assertEqual(order(sort="name"), ["Beta Ch", "Chan A", "zeta ch"])           # case-insensitive
        # newest video per channel: Beta Ch ("nothumb" was added last), zeta ch, then Chan A
        self.assertEqual(order(sort="recent"), ["Beta Ch", "zeta ch", "Chan A"])
        self.assertEqual(order(sort="bogus"), order())                                   # falls back

    def test_cards_link_to_the_filtered_library_and_random_buttons_exist(self):
        html = self.page().content.decode()
        self.assertIn("/library/?channel=Chan%20A", html)
        self.assertIn('id="randChannel"', html)
        self.assertIn("5 videos", html)
        self.assertIn("1 video<", html.replace("</div>", "<"))

    def test_login_required_and_nav_entry(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("channels")).status_code, 302)
        self.client.force_login(self.viewer)
        shell = self.client.get(reverse("library")).content.decode()
        self.assertEqual(shell.count('data-nav="channels"'), 2)             # top nav + bottom tab bar

    def test_library_filter_api_and_random_respect_channel(self):
        api = self.client.get(reverse("api_videos"), {"channel": "Beta Ch"}).json()
        self.assertEqual(api["total"], 2)
        self.assertEqual(self.client.get(reverse("api_videos"), {"channel": "nobody"}).json()["total"], 0)
        for _ in range(5):
            r = self.client.get(reverse("random_video"), {"channel": "zeta ch", "json": "1"}).json()
            self.assertEqual(r["id"], self.vids["z1"].pk)
        lib = self.client.get(reverse("library"), {"channel": "Chan A", "tag": "x"}).content.decode()
        self.assertIn("Channel: Chan A", lib)
        self.assertIn("channel=Chan%20A", lib)                              # kept by sort/fav/random/tag links
        self.assertIn("channel=", self.client.get(reverse("library"), {"channel": "Chan A"}).content.decode().split("data-random")[1])


class PlayerToolsTests(AuthTestCase):
    def test_file_path_thumbnail_and_rename_live_in_a_collapsed_block(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("library")).content.decode()
        self.assertIn('<details class="ph-utils">', html)
        block = html.split('<details class="ph-utils">')[1].split("</details>")[0]
        for needle in ("phRemotePath", "phThumbBtn", "phRenameBtn"):
            self.assertIn(needle, block)
        self.assertNotIn(" open", html.split('<details class="ph-utils">')[1][:5])

    def test_source_link_is_stamped_with_the_current_time(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("library")).content.decode()
        self.assertIn('id="phSrc"', html)
        self.assertIn("function sourceAt(url,sec)", html)
        self.assertIn("src.href=sourceAt(base,curTime())", html)


# ---- Round 6: not-a-short, preview/storyboard, cookie health, player markup ----

class NotShortTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.vert = _make_video(title="vert", width=720, height=1280, duration_seconds=600)
        self.wide = _make_video(title="wide", width=1920, height=1080, duration_seconds=600)
        self.client.force_login(self.owner)

    def toggle(self, v):
        return self.client.post(reverse("toggle_not_short", args=[v.pk]))

    def test_toggle_flips_everything_that_depends_on_being_a_short(self):
        self.assertTrue(self.vert.is_portrait)
        self.assertFalse(self.vert.remembers_position)
        r = self.toggle(self.vert).json()
        self.assertEqual((r["not_short"], r["is_portrait"], r["remember_position"]), (True, False, True))
        d = self.client.get(reverse("watch_api", args=[self.vert.pk])).json()
        self.assertEqual((d["is_portrait"], d["is_vertical"], d["not_short"], d["remember_position"]),
                         (False, True, True, True))
        shorts = self.client.get(reverse("shorts")).content.decode()
        self.assertNotIn(f"/watch/{self.vert.pk}/", shorts)
        # ...and back again
        r = self.toggle(self.vert).json()
        self.assertEqual((r["not_short"], r["is_portrait"]), (False, True))
        self.assertIn(f"/watch/{self.vert.pk}/", self.client.get(reverse("shorts")).content.decode())

    def test_only_vertical_videos_can_be_toggled(self):
        r = self.toggle(self.wide)
        self.assertEqual(r.status_code, 400)
        self.wide.refresh_from_db()
        self.assertFalse(self.wide.not_short)

    def test_owner_only(self):
        self.client.force_login(self.viewer)
        self.assertNotEqual(self.toggle(self.vert).status_code, 200)
        self.vert.refresh_from_db()
        self.assertFalse(self.vert.not_short)

    def test_marked_video_resumes_like_a_normal_one(self):
        PlaybackState.objects.create(video=self.vert, user=self.owner, position_seconds=300)
        titles = lambda: {v.title for v in self.client.get(reverse("home")).context["continue_watching"]}
        self.assertNotIn("vert", titles())                          # a short: never "continue"
        self.toggle(self.vert)
        self.assertIn("vert", titles())
        self.client.post(reverse("save_progress", args=[self.vert.pk]), {"position": "42"})
        self.assertEqual(PlaybackState.objects.get(video=self.vert, user=self.owner).position_seconds, 42)

    def test_refresh_still_treats_it_as_letterboxed(self):
        self.vert.not_short = True
        self.vert.save()
        self.assertTrue(self.vert.is_vertical)
        self.assertFalse(self.vert.is_portrait)


class PreviewApiTests(AuthTestCase):
    def test_preview_payload_and_hidden(self):
        v = _make_video(title="p", width=720, height=1280, thumbnail_path="/x.jpg")
        self.client.force_login(self.owner)
        d = self.client.get(reverse("api_preview", args=[v.pk])).json()
        self.assertEqual((d["id"], d["hls"], d["is_portrait"]), (v.pk, False, True))
        self.assertEqual(d["stream_url"], f"/stream/{v.pk}/")
        mkv = _make_video(title="m", ext="mkv", browser_playable=False)
        self.assertTrue(self.client.get(reverse("api_preview", args=[mkv.pk])).json()["hls"])
        v.hidden = True
        v.save()
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("api_preview", args=[v.pk])).status_code, 404)


@override_settings(THUMBNAIL_DIR=tempfile.mkdtemp())
class StoryboardTests(AuthTestCase):
    def make_clip(self, seconds=12):
        out = tempfile.mkstemp(suffix=".mp4")[1]
        os.remove(out)
        code, _, err = services._run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                                      f"testsrc=duration={seconds}:size=320x180:rate=10",
                                      "-g", "10", "-pix_fmt", "yuv420p", out])
        self.assertEqual(code, 0, err)
        self.addCleanup(lambda: os.path.exists(out) and os.remove(out))
        return out

    def test_build_serve_and_rebuild_on_change(self):
        clip = self.make_clip(12)
        v = _make_video(file_path=clip, rel_path="c.mp4", filename="c.mp4", width=320, height=180,
                        duration_seconds=12)
        meta = services.build_storyboard(v)
        self.assertEqual((meta["interval"], meta["count"], meta["cols"], meta["w"], meta["h"]),
                         (2, 6, 10, 160, 90))
        img, mp, lock = services._sb_paths(v)
        self.assertTrue(os.path.getsize(img) > 500)
        self.assertFalse(os.path.exists(lock))                      # lock released
        self.assertEqual(services.storyboard_meta(v)["count"], 6)
        self.client.force_login(self.owner)
        j = self.client.get(reverse("storyboard", args=[v.pk])).json()
        self.assertEqual((j["pending"], j["interval"]), (False, 2))
        r = self.client.get(reverse("storyboard_img", args=[v.pk]))
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "image/jpeg"))
        self.assertIn("max-age", r["Cache-Control"])
        # a changed source file invalidates it
        os.utime(clip, (time.time() + 100, time.time() + 100))
        self.assertIsNone(services.storyboard_meta(v))

    def test_single_flight_lock(self):
        clip = self.make_clip(6)
        v = _make_video(file_path=clip, rel_path="d.mp4", filename="d.mp4", width=320, height=180,
                        duration_seconds=6)
        lock = services._sb_paths(v)[2]
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        open(lock, "w").close()
        self.assertIsNone(services.build_storyboard(v))             # someone else is building
        self.assertTrue(os.path.exists(lock))                       # ...and we left their lock alone

    def test_endpoint_reports_pending_and_starts_a_build(self):
        v = _make_video(title="s", width=320, height=180, duration_seconds=60)
        self.client.force_login(self.owner)
        with mock.patch.object(services, "start_storyboard") as start:
            j = self.client.get(reverse("storyboard", args=[v.pk])).json()
        self.assertTrue(j["pending"])
        start.assert_called_once_with(v.pk)
        tiny = _make_video(title="t", duration_seconds=2)
        self.assertTrue(self.client.get(reverse("storyboard", args=[tiny.pk])).json()["none"])

    def test_frame_thumbs_are_browser_cacheable(self):
        clip = self.make_clip(6)
        v = _make_video(file_path=clip, rel_path="e.mp4", filename="e.mp4", duration_seconds=6)
        self.client.force_login(self.owner)
        r = self.client.get(reverse("frame_thumb", args=[v.pk, 0]))
        self.assertEqual(r.status_code, 200)
        self.assertIn("max-age", r["Cache-Control"])


class CookieHealthTests(DownloaderCase):
    def jar(self, login_exp):
        rows = ["# Netscape HTTP Cookie File"]
        for name, exp in (("SID", login_exp), ("VISITOR_INFO1_LIVE", 1)):
            rows.append("\t".join([".youtube.com", "TRUE", "/", "TRUE", str(exp), name, "v"]))
        with open(downloader.cookie_path(), "w") as f:
            f.write("\n".join(rows) + "\n")

    def test_expiry_reported_from_login_cookies_only(self):
        soon = int(time.time()) + 3 * 86400
        self.jar(soon)
        st = downloader.cookie_status()
        self.assertEqual(st["login_expires"], soon)
        self.assertFalse(st["expired"])
        self.jar(int(time.time()) - 10)
        self.assertTrue(downloader.cookie_status()["expired"])
        self.assertFalse(downloader.cookie_status().get("check") and True)

    def test_pasted_header_has_no_real_expiry(self):
        downloader.save_cookies("SID=x; HSID=y")                    # far-future placeholder
        st = downloader.cookie_status()
        self.assertIsNone(st["login_expires"])
        self.assertFalse(st["expired"])

    def run_probe(self, stderr="", stdout="", rc=0):
        downloader.save_cookies("SID=x; HSID=y")
        fake = mock.Mock(returncode=rc, stdout=stdout, stderr=stderr)
        with mock.patch.object(downloader, "ytdlp_available", return_value=True), \
             mock.patch.object(downloader.subprocess, "run", return_value=fake):
            return REAL_CHECK()

    def test_probe_outcomes(self):
        self.assertIs(self.run_probe(stdout='{"title": "Watch later"}')["ok"], True)
        self.assertIs(self.run_probe(stderr="ERROR: WL: YouTube said: The playlist does not exist.", rc=1)["ok"], False)
        self.assertIs(self.run_probe(stderr="WARNING: The provided YouTube account cookies are no longer valid.", rc=1)["ok"], False)
        self.assertIsNone(self.run_probe(stderr="ERROR: unable to download: connection reset", rc=1)["ok"])
        # the result is remembered for *this* cookie file only, and surfaced in the status
        self.assertIsNone(downloader.cookie_status()["check"]["ok"])
        time.sleep(0.05); downloader.save_cookies("SID=other; HSID=y")
        self.assertIsNone(downloader.cookie_status()["check"])

    def test_job_refuses_to_start_on_dead_cookies(self):
        downloader.save_cookies("SID=x; HSID=y")
        job = DownloadJob.objects.create(source=self.src, ids=json.dumps(["aaaaaaaaaaa"]), total=1)
        with mock.patch.object(downloader, "check_cookies",
                               return_value={"ok": False, "msg": downloader.COOKIE_BAD, "checked": time.time()}), \
             mock.patch.object(downloader, "build_cmd") as build:
            downloader._run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, DownloadJob.FAILED)
        self.assertIn("don't work anymore", job.summary)
        self.assertIn("Nothing was downloaded", job.summary)
        build.assert_not_called()

    def test_inconclusive_probe_does_not_block(self):
        downloader.save_cookies("SID=x; HSID=y")
        with mock.patch.object(downloader, "check_cookies", return_value={"ok": None, "checked": time.time()}):
            self.assertIsNone(downloader.cookies_ok_for_job())

    def test_recent_good_result_is_reused(self):
        downloader.save_cookies("SID=x; HSID=y")
        downloader._write_check(True, "Signed in")
        with mock.patch.object(downloader, "check_cookies") as probe:
            self.assertIsNone(downloader.cookies_ok_for_job())
        probe.assert_not_called()

    def test_start_job_refuses_expired_cookies_up_front(self):
        self.jar(int(time.time()) - 10)
        with mock.patch.object(downloader, "ytdlp_available", return_value=True):
            job, err = downloader.start_job(self.src, ["aaaaaaaaaaa"])
        self.assertIsNone(job)
        self.assertIn("don't work anymore", err)

    def test_running_job_is_stopped_when_yt_dlp_reports_dead_cookies(self):
        # yt-dlp's real behaviour: warn, then carry on logged out. We must not.
        script = ("import sys,time\n"
                  "mode, before, after, *ids = sys.argv[1:]\n"
                  "open(before,'w').write('\\n'.join(ids)+'\\n')\n"
                  "print('WARNING: [youtube] The provided YouTube account cookies are no longer valid.', flush=True)\n"
                  "time.sleep(30)\n")
        downloader.save_cookies("SID=x; HSID=y")
        job = DownloadJob.objects.create(source=self.src, ids=json.dumps(["aaaaaaaaaaa"]), total=1)

        def fake_build(ids_, out_dir, archive, cookie):
            before, after = os.path.join(self.ytdl, "b.txt"), os.path.join(self.ytdl, "a.txt")
            return ([sys.executable, "-c", script, "x", before, after, *ids_], before, after,
                    os.path.join(self.ytdl, "u.txt"))

        t0 = time.time()
        with mock.patch.object(downloader, "build_cmd", side_effect=fake_build), \
             mock.patch.object(downloader.services, "scan_library"):
            downloader._run_job(job.pk)
        job.refresh_from_db()
        self.assertLess(time.time() - t0, 20)                       # killed, not waited out
        self.assertEqual(job.status, DownloadJob.FAILED)
        self.assertIn("don't work anymore", job.summary)
        self.assertFalse(downloader.read_cookie_check()["ok"])      # the UI now shows the warning

    def test_cookies_state_and_test_endpoint(self):
        self.client.force_login(self.owner)
        downloader.save_cookies("SID=x; HSID=y")
        r = self.client.get(reverse("dl_tools"), HTTP_X_SPA="1").json()
        self.assertIn("login_expires", r["cookies"])
        with mock.patch.object(downloader, "check_cookies_async") as go:
            self.assertEqual(self.client.post(reverse("dl_cookies_check")).status_code, 200)
        go.assert_called_once()
        self.client.force_login(self.viewer)
        self.assertNotEqual(self.client.post(reverse("dl_cookies_check")).status_code, 200)



class PlayerMarkupTests(AuthTestCase):
    def html(self):
        self.client.force_login(self.owner)
        return self.client.get(reverse("library")).content.decode()

    def test_notes_sit_above_the_playlist_block(self):
        h = self.html()
        self.assertLess(h.index('class="notes-section"'), h.index('class="pl-form"'))
        self.assertLess(h.index('class="tech"'), h.index('class="notes-section"'))
        self.assertLess(h.index('class="pl-form"'), h.index('class="ph-utils"'))

    def test_player_search_is_a_real_form_like_the_header_one(self):
        h = self.html()
        self.assertIn('id="phSearchForm"', h)
        self.assertIn('enterkeyhint="search"', h)
        self.assertIn("minimize({keepUrl:true})", h)

    def test_shorts_feed_and_polish_hooks_are_present(self):
        h = self.html()
        for needle in ('id="phNbPrev"', 'id="phNbNext"', "function commitSlide", "function shouldCommit",
                       "/api/preview/", "homeflix_volume", "/storyboard/", "setLoading(", "railNotShortBtn",
                       "e.movementX===0&&e.movementY===0"):
            self.assertIn(needle, h, needle)
        self.assertNotIn("shortsSlide(", h)
