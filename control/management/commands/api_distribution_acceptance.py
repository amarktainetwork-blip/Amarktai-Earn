import json

from django.core.management.base import BaseCommand, CommandError

from control.services.api_distribution import api_distribution_acceptance_report


class Command(BaseCommand):
    help = "Report the multi-market commercial API distribution readiness gate."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=("json", "text"), default="text")
        parser.add_argument("--fail-on", choices=("FAIL",), default="FAIL")

    def handle(self, *args, **options):
        report = api_distribution_acceptance_report()
        if options["format"] == "json":
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        else:
            self.stdout.write(f"{report['name']}: {report['status']}")
            for row in report["criteria"]:
                self.stdout.write(f"- {row['name']}: {row['status']}")
        if report["status"] == options["fail_on"]:
            raise CommandError("API distribution acceptance failed")
