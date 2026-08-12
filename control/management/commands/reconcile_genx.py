import json

from django.core.management.base import BaseCommand, CommandError

from control.services.genx_recovery import GenXRecoveryError, recover_completed_genx_call
from gateways.genx.client import GenXError
from gateways.genx.service import GenXGateway


class Command(BaseCommand):
    help = "Reconcile existing GenX calls without replay; optionally recover one completed execution end-to-end."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--call-id", default="")
        parser.add_argument("--remote-job-id", default="")
        parser.add_argument("--format", choices=("text", "json"), default="text")

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1 or limit > 1000:
            raise CommandError("limit must be between 1 and 1000")
        call_id = str(options.get("call_id") or "").strip()
        remote_job_id = str(options.get("remote_job_id") or "").strip()
        try:
            if call_id:
                result = recover_completed_genx_call(
                    call_id,
                    expected_remote_job_id=remote_job_id,
                )
            else:
                if remote_job_id:
                    raise CommandError("--remote-job-id requires --call-id")
                result = GenXGateway().reconcile_pending(limit=limit)
        except (GenXError, GenXRecoveryError, ValueError) as exc:
            raise CommandError(f"GenX reconciliation failed: {exc}") from exc
        if options["format"] == "json":
            self.stdout.write(json.dumps(result, sort_keys=True, default=str))
        else:
            self.stdout.write(self.style.SUCCESS(f"GenX reconciliation: {result}"))
