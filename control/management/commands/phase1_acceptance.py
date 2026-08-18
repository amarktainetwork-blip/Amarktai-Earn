from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from control.services.phase1_acceptance import phase1_acceptance_report


class Command(BaseCommand):
    help = "Fail-closed Phase 1 execution-engine acceptance gate. No provider or marketplace mutation is performed."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=("text", "json"), default="text")
        parser.add_argument("--fail-on", choices=("FAIL", "NEVER"), default="FAIL")

    def handle(self, *args, **options):
        report = phase1_acceptance_report()

        if options["format"] == "json":
            self.stdout.write(json.dumps(report, sort_keys=True, default=str))
        else:
            self.stdout.write(f"PHASE 1 EXECUTION ENGINE: {report['status']}")
            for row in report["checks"]:
                self.stdout.write(f"{row['status']:4} {row['name']} {row['details']}")

        if report["status"] == "FAIL" and options["fail_on"] == "FAIL":
            raise CommandError("Phase 1 execution-engine acceptance failed")
