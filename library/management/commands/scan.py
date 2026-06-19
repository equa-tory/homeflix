from django.core.management.base import BaseCommand
from library import services


class Command(BaseCommand):
    help = "Scan the library folder for new/changed/removed videos."

    def add_arguments(self, parser):
        parser.add_argument("--no-thumbs", action="store_true",
                            help="Skip thumbnail generation (faster).")

    def handle(self, *args, **opts):
        summary = services.scan_library(make_thumbs=not opts["no_thumbs"])
        if summary["error"]:
            self.stderr.write(self.style.ERROR(summary["error"]))
            return
        self.stdout.write(self.style.SUCCESS(
            f"Added {summary['added']}, updated {summary['updated']}, "
            f"missing {summary['missing']} (root: {summary['root']})"))
