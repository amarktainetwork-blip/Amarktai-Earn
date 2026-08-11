from django.core.management.base import BaseCommand

from control.services.product_factory import product_factory_cycle


class Command(BaseCommand):
    help = "Synchronize the capability monetization matrix and run one bounded owned-product candidate cycle."

    def handle(self, *args, **options):
        result = product_factory_cycle()
        self.stdout.write(self.style.SUCCESS(str(result)))
