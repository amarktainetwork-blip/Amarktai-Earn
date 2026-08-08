from django.core.management.base import BaseCommand, CommandError

from planning.services import PlanningError, stage_local_job_asset


class Command(BaseCommand):
    help = "Stage and hash a local source file for an acquired job from approved job/upload storage."

    def add_arguments(self, parser):
        parser.add_argument("--job", required=True)
        parser.add_argument("--path", required=True)
        parser.add_argument("--source", default="upload")
        parser.add_argument("--external-id", default="")
        parser.add_argument("--role", default="source")

    def handle(self, *args, **options):
        try:
            asset = stage_local_job_asset(
                job_id=options["job"],
                path=options["path"],
                source=options["source"],
                external_id=options["external_id"],
                semantic_role=options["role"],
            )
        except (PlanningError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"asset_id={asset.id} sha256={asset.sha256} status={asset.status}"))
