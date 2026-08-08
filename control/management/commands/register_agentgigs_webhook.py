import os

from django.core.management.base import BaseCommand, CommandError

from control.models import MarketplaceCredential
from control.secrets import encrypt_secret
from control.services.agentgigs import configured_adapter, ensure_marketplace
from markets.agentgigs.client import AgentGigsError


class Command(BaseCommand):
    help = "Register the production AgentGigs webhook and store its generated secret encrypted when returned by the API."

    def add_arguments(self, parser):
        parser.add_argument("--url", default="https://earn.amarktai.co.za/webhooks/agentgigs/")

    def handle(self, *args, **options):
        try:
            result = configured_adapter().register_webhook(options["url"])
        except AgentGigsError as exc:
            raise CommandError(f"AgentGigs webhook registration failed: {exc}") from exc
        secret = str(result.get("secret") or result.get("webhook_secret") or "")
        if secret:
            market = ensure_marketplace()
            MarketplaceCredential.objects.filter(
                marketplace=market,
                credential_type="webhook_secret",
                active=True,
            ).update(active=False)
            MarketplaceCredential.objects.create(
                marketplace=market,
                credential_type="webhook_secret",
                encrypted_value=encrypt_secret(secret),
                active=True,
            )
            self.stdout.write(self.style.SUCCESS("AgentGigs webhook registered; generated secret stored encrypted."))
        else:
            self.stdout.write(self.style.WARNING("Webhook registered, but the API response did not expose a secret field. Configure AGENTGIGS_WEBHOOK_SECRET from the platform-provided secret before accepting deliveries."))
        self.stdout.write(f"webhook_url={options['url']}")
