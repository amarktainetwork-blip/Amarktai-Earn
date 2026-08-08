from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from control.services.profit_brain import refresh_profit_intelligence


class Command(BaseCommand):
    help = "Persist capacity, rolling performance, and growth-target intelligence."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=("text", "json"), default="text")

    def handle(self, *args, **options):
        result = refresh_profit_intelligence()
        if options["format"] == "json":
            self.stdout.write(json.dumps(result, sort_keys=True, default=str))
            return
        self.stdout.write(
            f"growth={result['growth_status']} performance_snapshots={result['performance_snapshots']} "
            f"capacity_snapshot={result['capacity_snapshot_id']}"
        )
