import json

from django.core.management.base import BaseCommand

from control.services.channel_onboarding import reapply_priority_channel_onboarding


class Command(BaseCommand):
    help = "Reapply persisted non-banking priority-channel onboarding evidence after catalog bootstrap."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(reapply_priority_channel_onboarding(), sort_keys=True))
