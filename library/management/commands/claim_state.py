from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from library.models import PlaybackState, WatchEvent, Setting, UserPref

# Keep in sync with _PER_USER_SETTING_KEYS in
# library/migrations/0009_per_user_state.py.
PER_USER_SETTING_KEYS = ("theme", "autoplay", "repeat", "shuffle", "loop")


class Command(BaseCommand):
    help = ("Attach pre-auth (user=NULL) playback/history rows -- and the "
            "legacy global theme/autoplay/repeat/shuffle Setting values -- "
            "to the given user. Recovery path for when 'migrate' ran before "
            "any account existed (see 0009_per_user_state.py).")

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **opts):
        User = get_user_model()
        try:
            user = User.objects.get(username=opts["username"])
        except User.DoesNotExist:
            raise CommandError(f"No such user: {opts['username']!r}")

        n_pb = PlaybackState.objects.filter(user__isnull=True).update(user=user)
        n_we = WatchEvent.objects.filter(user__isnull=True).update(user=user)
        for row in Setting.objects.filter(key__in=PER_USER_SETTING_KEYS):
            UserPref.objects.update_or_create(
                user=user, key=row.key, defaults={"value": row.value})

        self.stdout.write(self.style.SUCCESS(
            f"Claimed {n_pb} playback row(s), {n_we} watch event(s) for "
            f"{user.username!r}."))
