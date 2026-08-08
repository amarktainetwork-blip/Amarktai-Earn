from django.core.management.base import BaseCommand, CommandError

from markets.catalog import BY_SLUG
from control.services.markets import bootstrap_market_integrations, sync_market_discovery


class Command(BaseCommand):
    help = "Run one bounded, adapter-driven discovery pass. This never acquires work."

    def add_arguments(self, parser):
        parser.add_argument("market", choices=sorted(BY_SLUG))
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        bootstrap_market_integrations()
        try:
            result = sync_market_discovery(options["market"], limit=options["limit"])
        except Exception as exc:
            raise CommandError(f"Discovery failed safely: {exc.__class__.__name__}") from exc
        self.stdout.write(str(result))
