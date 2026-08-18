import json

from django.core.management.base import BaseCommand, CommandError

from control.services.launch_acceptance import launch_acceptance_report


class Command(BaseCommand):
    help = "Compositional final launch gate. Performs no provider, payment, publication, or deployment mutation."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=("text", "json"), default="text")
        parser.add_argument("--ci-proven", action="store_true")
        parser.add_argument("--fail-on", choices=("FAIL", "NEVER"), default="FAIL")

    def handle(self, *args, **options):
        report = launch_acceptance_report(ci_proven=bool(options["ci_proven"]))
        if options["format"] == "json":
            self.stdout.write(json.dumps(report, sort_keys=True, default=str))
        else:
            self.stdout.write(f"FINAL LAUNCH ACCEPTANCE: {report['status']}")
            for row in report["criteria"]:
                self.stdout.write(f"{row['status']:28} {row['name']}")
        if report["status"] == "FAIL" and options["fail_on"] == "FAIL":
            raise CommandError("Final launch acceptance failed")
