from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from markets.revenue_catalog import bootstrap_revenue_market_catalog


class Command(BaseCommand):
    help = "Idempotently materialize the fail-closed revenue market catalogue."

    def handle(self, *args, **options):
        result = bootstrap_revenue_market_catalog()
        self.stdout.write(json.dumps(result, sort_keys=True))
