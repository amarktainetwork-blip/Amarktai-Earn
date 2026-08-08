from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from control.services.v1_acceptance import build_acceptance_report


class Command(BaseCommand):
    help = "Emit the honest V1 acceptance report without converting external proof into PASS."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=("text", "json"), default="text")
        parser.add_argument("--ci-proven", action="store_true", help="Use only as the final step of the complete sequential CI workflow.")
        parser.add_argument("--fail-on", choices=("FAIL", "BLOCKED"), default="FAIL")

    def handle(self, *args, **options):
        report = build_acceptance_report(ci_proven=bool(options["ci_proven"]))
        if options["format"] == "json":
            self.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")))
        else:
            self.stdout.write(f"V1 ACCEPTANCE: {report['overall_status']}")
            for row in report["criteria"]:
                self.stdout.write(f"{row['status']:<23} {row['id']}: {row['evidence']}")
                if row["operator_action"]:
                    self.stdout.write(f"  ACTION: {row['operator_action']}")
        should_fail = report["counts"]["FAIL"] > 0
        if options["fail_on"] == "BLOCKED":
            should_fail = should_fail or report["counts"]["BLOCKED"] > 0
        if should_fail:
            raise CommandError(f"V1 acceptance contains {options['fail_on']} conditions")
