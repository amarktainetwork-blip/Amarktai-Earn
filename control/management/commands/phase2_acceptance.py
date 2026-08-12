from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from control.services.phase2_acceptance import phase2_acceptance_report


class Command(BaseCommand):
    help = "Fail-closed Phase 2 capability acceptance gate. Performs no paid provider mutation."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=("text", "json"), default="text")
        parser.add_argument("--fail-on", choices=("FAIL", "NEVER"), default="FAIL")

    def handle(self, *args, **options):
        report = phase2_acceptance_report()
        if options["format"] == "json":
            self.stdout.write(json.dumps(report, sort_keys=True, default=str))
        else:
            summary = report["summary"]
            self.stdout.write(f"PHASE 2 CAPABILITIES: {report['status']}")
            self.stdout.write(
                " ".join(
                    f"{name}={summary[name]}"
                    for name in ("REGISTERED_OPERATIONS", "PASS", "READY_FOR_CREDENTIAL", "FAIL", "PARTIAL", "UNKNOWN", "UNREGISTERED")
                )
            )
            for row in report["rows"]:
                self.stdout.write(f"{row['status']:20} {row['kind']:18} {row['name']}")
        if report["status"] == "FAIL" and options["fail_on"] == "FAIL":
            raise CommandError("Phase 2 capability acceptance failed")
