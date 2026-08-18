import os
import time

from django.core.management.base import BaseCommand, CommandError

from control.services.inbound_controller import revenue_controller_cycle
from control.services.recovery import heartbeat
from control.tasks import autonomous_income_bounded_work_task, autonomous_income_daily_task, autonomous_income_frequent_task


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

    def _dealwork_cycle(self, *, limit: int) -> dict:
        if os.getenv("DEALWORK_WATCHER_ENABLED", "0") != "1":
            return {"enabled": False, "mutation_performed": False}

        from control.services.dealwork_runtime import run_dealwork_cycle

        return run_dealwork_cycle(limit=limit)

    def handle(self, *args, **options):
        interval = max(15, options["interval"] or int(os.getenv("REVENUE_WATCHER_INTERVAL_SECONDS", "60")))
        limit = max(1, min(int(options["limit"]), 500))
        last_daily = 0.0
        while True:
            try:
                heartbeat("revenue-watcher", details={"phase": "cycle"})
                autonomous = {
                    "frequent": autonomous_income_frequent_task(),
                    "bounded": autonomous_income_bounded_work_task(),
                }
                now = time.monotonic()
                if last_daily == 0.0 or now - last_daily >= 86400:
                    autonomous["daily"] = autonomous_income_daily_task()
                    last_daily = now
                result = {
                    "agentgigs": self._agentgigs_cycle(limit=limit),
                    "dealwork": self._dealwork_cycle(limit=limit),
                    "seller_inbound": revenue_controller_cycle(limit=limit),
                    "autonomous_income": autonomous,
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
