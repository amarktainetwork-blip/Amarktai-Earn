import json

from django.core.management.base import BaseCommand, CommandError

from control.services.autonomous_income_acceptance import autonomous_income_acceptance_report


class Command(BaseCommand):
    help = "Verify Webdock-safe autonomous income engineering and readiness truth."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=("json", "text"), default="text")

    def handle(self, *args, **options):
        report = autonomous_income_acceptance_report()
        if options["format"] == "json":
            self.stdout.write(json.dumps(report, indent=2, default=str, sort_keys=True))
        else:
            for row in report["criteria"]:
                self.stdout.write(f"{row['criterion']}: {row['status']}")
            self.stdout.write(f"OVERALL: {report['status']}")
        if report["status"] != "PASS":
            raise CommandError("autonomous income acceptance failed")
