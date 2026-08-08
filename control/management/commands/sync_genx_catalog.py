from django.core.management.base import BaseCommand, CommandError

from gateways.genx.client import GenXError
from gateways.genx.service import GenXGateway


class Command(BaseCommand):
    help = "Refresh the controller-owned GenX model catalog, pricing snapshots and credit balance."

    def add_arguments(self, parser):
        parser.add_argument("--category", choices=["text", "image", "video", "voice", "audio"], default=None)

    def handle(self, *args, **options):
        try:
            result = GenXGateway().sync_catalog(options["category"])
        except (GenXError, ValueError) as exc:
            raise CommandError(f"GenX catalog sync failed: {exc}") from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"GenX catalog synced: models={result['models_seen']} available_credits={result['available_credits']}"
            )
        )
