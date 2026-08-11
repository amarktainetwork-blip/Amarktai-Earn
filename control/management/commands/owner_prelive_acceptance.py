import json

from django.core.management.base import BaseCommand, CommandError

from control.services.prelive_acceptance import prelive_acceptance_report


class Command(BaseCommand):
    help = "Emit the deterministic owner pre-live repository acceptance and blocker classification report."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        report = prelive_acceptance_report()
        rendered = json.dumps(report, indent=2, sort_keys=True, default=str)
        self.stdout.write(rendered)
        if report["code_blocker_count"]:
            raise CommandError(f"{report['code_blocker_count']} material CODE_BLOCKER criteria remain")
