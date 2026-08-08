from django.core.management.base import BaseCommand

from control.services.markets import bootstrap_market_integrations


class Command(BaseCommand):
    help = "Persist official V1 market adapter capabilities, sources, and fail-closed blockers."

    def handle(self, *args, **options):
        result = bootstrap_market_integrations()
        self.stdout.write(self.style.SUCCESS(f"Market integrations persisted: {result}"))
