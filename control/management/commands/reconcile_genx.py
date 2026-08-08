from django.core.management.base import BaseCommand, CommandError

from gateways.genx.client import GenXError
from gateways.genx.service import GenXGateway


class Command(BaseCommand):
    help = "Reconcile submitted/unknown GenX calls that already have remote job IDs without replaying them."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1 or limit > 1000:
            raise CommandError("limit must be between 1 and 1000")
        try:
            result = GenXGateway().reconcile_pending(limit=limit)
        except (GenXError, ValueError) as exc:
            raise CommandError(f"GenX reconciliation failed: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(f"GenX reconciliation: {result}"))
