from django.core.management.base import BaseCommand

from control.services.integration_reconciliation import reconcile_integrations


class Command(BaseCommand):
    help = "Run one bounded credential-aware external integration reconciliation cycle."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=25)

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS(str(reconcile_integrations(limit_per_integration=options["limit"]))))
