from django.core.management.base import BaseCommand, CommandError

from control.services.agentgigs import configured_adapter, run_cycle
from markets.agentgigs.client import AgentGigsError


class Command(BaseCommand):
    help = "Run one bounded AgentGigs control cycle: P0 webhooks, active jobs, discovery, scoring and gated acquisition."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        limit = max(1, min(options["limit"], 500))
        try:
            result = run_cycle(configured_adapter(), limit=limit)
        except (AgentGigsError, ValueError) as exc:
            raise CommandError(f"AgentGigs sync failed: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(str(result)))
