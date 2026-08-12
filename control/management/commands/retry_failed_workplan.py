from django.core.management.base import BaseCommand, CommandError

from planning.retry import FailedWorkPlanRetryError, retry_failed_work_plan


class Command(BaseCommand):
    help = "Reopen and optionally queue a FAILED work plan after a corrected local defect, with replay-safety checks."

    def add_arguments(self, parser):
        parser.add_argument("plan_id", type=int)
        parser.add_argument("--reason", required=True)
        parser.add_argument("--actor", default="operator-recovery")
        parser.add_argument(
            "--no-enqueue",
            action="store_true",
            help="Prepare the failed plan as READY without enqueueing it.",
        )

    def handle(self, *args, **options):
        try:
            plan = retry_failed_work_plan(
                options["plan_id"],
                reason=options["reason"],
                actor=options["actor"],
                enqueue=not options["no_enqueue"],
            )
        except FailedWorkPlanRetryError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"plan={plan.id} job={plan.job_id} status={plan.status} execution_attempts={plan.execution_attempts}"
            )
        )
