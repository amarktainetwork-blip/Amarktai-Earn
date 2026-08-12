from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from control.services.phase3_acceptance import phase3_acceptance_report


class Command(BaseCommand):
    help = "Fail-closed Phase 3 engineering readiness gate. No provider, payment, market, GitHub, or deployment mutation is performed."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=("text", "json"), default="text")
        parser.add_argument("--ci-proven", action="store_true")
        parser.add_argument("--fail-on", choices=("FAIL", "NEVER"), default="FAIL")

    def handle(self, *args, **options):
        report = phase3_acceptance_report(ci_proven=bool(options["ci_proven"]))
        if options["format"] == "json":
            self.stdout.write(json.dumps(report, sort_keys=True, default=str))
        else:
            self.stdout.write(f"PHASE 3 READY FOR KEYS: {report['status']}")
            for row in report["core"]:
                self.stdout.write(f"{row['status']:4} {row['name']}")
            summary = report["summary"]
            self.stdout.write(
                " ".join(
                    f"{key}={summary[key]}"
                    for key in (
                        "FAILURES", "PARTIAL", "UNKNOWN", "CONNECTIONS_TOTAL",
                        "CONNECTIONS_CONNECTED", "CONNECTIONS_READY_FOR_CREDENTIAL",
                        "CONNECTIONS_READY_FOR_OWNER_ACTION", "EXTERNAL_PRODUCTION_PROOFS",
                    )
                )
            )
        if report["status"] == "FAIL" and options["fail_on"] == "FAIL":
            raise CommandError("Phase 3 ready-for-keys acceptance failed")
