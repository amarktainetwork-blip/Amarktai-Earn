import os
import time

from django.core.management.base import BaseCommand, CommandError

from control.services.inbound_controller import revenue_controller_cycle
from control.services.recovery import heartbeat


class Command(BaseCommand):
    help = "Run bounded revenue cycles for source markets and seller-side inbound orders."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--interval", type=int, default=None)

    def _agentgigs_cycle(self, *, limit: int) -> dict:
        if os.getenv("AGENTGIGS_WATCHER_ENABLED", "0") != "1":
            return {"enabled": False, "mutation_performed": False}
        from control.services.agentgigs import configured_adapter, run_cycle
        from control.services.agentgigs_assets import sync_awarded_agentgigs_assets
        from planning.coding import dispatch_coding_jobs
        from planning.services import dispatch_awarded_jobs

        adapter = configured_adapter()
        result = run_cycle(adapter, limit=limit)
        result["assets"] = sync_awarded_agentgigs_assets(adapter, limit=limit)
        result["dispatch"] = dispatch_awarded_jobs(marketplace_slug="agentgigs", limit=limit)
        result["coding_dispatch"] = dispatch_coding_jobs(marketplace_slug="agentgigs", limit=limit)
        result["enabled"] = True
        return result

    def handle(self, *args, **options):
        interval = max(15, options["interval"] or int(os.getenv("REVENUE_WATCHER_INTERVAL_SECONDS", "60")))
        limit = max(1, min(int(options["limit"]), 500))
        while True:
            try:
                heartbeat("revenue-watcher", details={"phase": "cycle"})
                result = {
                    "agentgigs": self._agentgigs_cycle(limit=limit),
                    "seller_inbound": revenue_controller_cycle(limit=limit),
                }
                heartbeat("revenue-watcher", details=result)
                self.stdout.write(self.style.SUCCESS(str(result)))
            except Exception as exc:
                heartbeat("revenue-watcher", details={"phase": "error", "error_code": exc.__class__.__name__})
                if options["once"]:
                    raise CommandError(f"Revenue watcher cycle failed: {exc}") from exc
                self.stderr.write(self.style.WARNING(f"Revenue watcher cycle failed: {exc.__class__.__name__}"))
            if options["once"]:
                return
            time.sleep(interval)
