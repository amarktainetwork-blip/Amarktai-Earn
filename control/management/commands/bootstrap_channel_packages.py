import json

from django.core.management.base import BaseCommand

from control.services.channel_packages import sync_priority_channel_packages


class Command(BaseCommand):
    help = "Persist fail-closed draft packages for the priority earning channels."

    def handle(self, *args, **options):
        result = sync_priority_channel_packages()
        self.stdout.write(json.dumps(result, sort_keys=True))
