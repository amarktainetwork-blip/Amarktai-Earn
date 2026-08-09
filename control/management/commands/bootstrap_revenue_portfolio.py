from django.core.management.base import BaseCommand

from control.services.seller_services import sync_candidate_service_offerings
from markets.revenue_catalog import bootstrap_revenue_market_catalog


class Command(BaseCommand):
    help = "Persist the disabled, fail-closed two-sided revenue market and service candidate catalog."

    def handle(self, *args, **options):
        markets = bootstrap_revenue_market_catalog()
        offerings = sync_candidate_service_offerings()
        self.stdout.write(self.style.SUCCESS(f"revenue markets={markets} service offerings={offerings}"))
