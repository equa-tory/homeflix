import threading
import logging
from django.apps import AppConfig

logger = logging.getLogger("library")


class LibraryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "library"

    def ready(self):
        import sys, os

        # Skip during management commands that don't serve HTTP
        skip = {"migrate", "makemigrations", "collectstatic",
                "shell", "scan", "test", "check", "showmigrations", "dbshell"}
        subcmd = sys.argv[1] if len(sys.argv) > 1 else ""
        if subcmd in skip:
            return

        # Django's dev server forks once (reloader) then again (actual server).
        # RUN_MAIN=true is set only in the actual server process.
        if subcmd == "runserver" and os.environ.get("RUN_MAIN") != "true":
            return

        self._schedule_scan()

    def _schedule_scan(self):
        from django.conf import settings
        minutes = getattr(settings, "SCAN_INTERVAL_MINUTES", 30)
        interval = minutes * 60

        def run():
            try:
                from library.services import scan_library
                r = scan_library(make_thumbs=True)
                if r.get("error"):
                    logger.warning("Background scan: %s", r["error"])
                else:
                    logger.info("Background scan: +%d added, %d missing",
                                r["added"], r["missing"])
            except Exception as exc:
                logger.error("Background scan failed: %s", exc)
            finally:
                t = threading.Timer(interval, run)
                t.daemon = True
                t.start()

        t = threading.Timer(interval, run)
        t.daemon = True
        t.start()
        logger.info("Background scan every %d min", minutes)
