from django.core.management.base import BaseCommand

from control.services.api_distribution import bootstrap_api_distribution
from control.services.commercial_api import bootstrap_commercial_catalog
from control.services.commercial_intelligence import bootstrap_commercial_packages


class Command(BaseCommand):
    help = "Idempotently materialize the engineering-proven commercial API, channel economics, distribution exports, and reusable package catalog."

    def handle(self, *args, **options):
        api = bootstrap_commercial_catalog()
        distribution = bootstrap_api_distribution()
        packages = bootstrap_commercial_packages()
        self.stdout.write(self.style.SUCCESS(f"commercial catalog ready: api={api} distribution={distribution} packages={packages}"))