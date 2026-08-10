from django.core.management.base import BaseCommand

from control.services.channel_commercial import bootstrap_channel_commercial_pricing


class Command(BaseCommand):
    help = "Idempotently prepare fail-closed commercial pricing for priority channel packages."

    def handle(self, *args, **options):
        result = bootstrap_channel_commercial_pricing()
        self.stdout.write(__import__("json").dumps(result, sort_keys=True))
