import os
import time

from django.core.management.base import BaseCommand

from control.services.recovery import run_recovery_cycle


class Command(BaseCommand):
    help = "Run bounded persistent-state recovery, reconciliation and retention cleanup cycles."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=int, default=None)

    def handle(self, *args, **options):
        interval = max(30, options["interval"] or int(os.getenv("WATCHDOG_INTERVAL_SECONDS", "60")))
        while True:
            self.stdout.write(self.style.SUCCESS(str(run_recovery_cycle())))
            if options["once"]:
                return
            time.sleep(interval)
