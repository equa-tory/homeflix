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
        self._schedule_backup()

    def _schedule_scan(self):
        from django.conf import settings
        minutes = getattr(settings, "SCAN_INTERVAL_MINUTES", 30)
        interval = minutes * 60

        def run():
            try:
                from django.db import connection
                from library.backup import claim
                from library.services import scan_library
                # Every gunicorn worker runs this timer; only one should scan
                # per interval (concurrent scans raced on inserting the same
                # new file -- UNIQUE constraint failed: library_video.file_path).
                if claim("scan", interval * 0.9):
                    r = scan_library(make_thumbs=True)
                    if r.get("error"):
                        logger.warning("Background scan: %s", r["error"])
                    else:
                        logger.info("Background scan: +%d added, %d missing",
                                    r["added"], r["missing"])
                    for w in r.get("warnings", []):
                        logger.warning("Background scan: %s", w)
            except Exception as exc:
                logger.error("Background scan failed: %s", exc)
            finally:
                try:
                    connection.close()
                except Exception:
                    pass
                t = threading.Timer(interval, run)
                t.daemon = True
                t.start()

        t = threading.Timer(interval, run)
        t.daemon = True
        t.start()
        logger.info("Background scan every %d min", minutes)

    def _schedule_backup(self):
        """Check every 10 minutes whether an automatic backup is due (see
        library/backup.py). The first check is a minute after start so a
        backup missed while the server was down is made up promptly."""
        def run():
            try:
                from library import backup
                backup.maybe_run()
            except Exception as exc:
                logger.error("Backup check failed: %s", exc)
            finally:
                try:
                    from django.db import connection
                    connection.close()
                except Exception:
                    pass
                t = threading.Timer(600, run)
                t.daemon = True
                t.start()

        t = threading.Timer(60, run)
        t.daemon = True
        t.start()
