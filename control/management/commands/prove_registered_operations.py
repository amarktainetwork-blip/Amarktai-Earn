from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from workers.registry import capability_coverage


class Command(BaseCommand):
    help = "Prove every canonical registered operation contract without paid or marketplace side effects."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=("json", "text"), default="json")
        parser.add_argument("--fail-on", choices=("FAIL", "NEVER"), default="FAIL")

    def handle(self, *args, **options):
        report = capability_coverage()
        if options["format"] == "json":
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        else:
            self.stdout.write(f"REGISTERED OPERATION PROOF: {report['status']}")
            for key, value in report["summary"].items():
                self.stdout.write(f"{key}={value}")
            for row in report["operations"]:
                blocker = f" blocker={row['owner_action_blocker']}" if row["owner_action_blocker"] else ""
                errors = f" errors={','.join(row['errors'])}" if row["errors"] else ""
                self.stdout.write(f"{row['status']} {row['operation']} worker={row['worker_class']}{blocker}{errors}")
        if report["status"] == "FAIL" and options["fail_on"] == "FAIL":
            raise CommandError("registered operation proof failed")
