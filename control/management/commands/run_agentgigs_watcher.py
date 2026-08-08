import os
import time

from django.core.management.base import BaseCommand, CommandError

from control.services.agentgigs import configured_adapter, run_cycle
from markets.agentgigs.client import AgentGigsError


class Command(BaseCommand):
    help = "Run bounded AgentGigs synchronization/acquisition cycles with P0 revenue-protection work first."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--interval", type=int, default=None)

    def handle(self, *args, **options):
        interval = options["interval"] or int(os.getenv("AGENTGIGS_WATCHER_INTERVAL_SECONDS", "60"))
        interval = max(15, interval)
        limit = max(1, min(int(options["limit"]), 500))
        while True:
            try:
                result = run_cycle(configured_adapter(), limit=limit)
                self.stdout.write(self.style.SUCCESS(str(result)))
            except (AgentGigsError, ValueError) as exc:
                if options["once"]:
                    raise CommandError(f"AgentGigs watcher cycle failed: {exc}") from exc
                self.stderr.write(self.style.WARNING(f"AgentGigs watcher cycle failed: {exc.__class__.__name__}"))
            if options["once"]:
                return
            time.sleep(interval)
