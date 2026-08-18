from django.core.management.base import BaseCommand, CommandError

from control.services.api_distribution import (
    API_MARKET_CHANNEL,
    ZYLA_CHANNEL,
    issue_marketplace_backend_key,
)


class Command(BaseCommand):
    help = "Owner-only one-time issuer for a product-scoped marketplace backend API key."

    def add_arguments(self, parser):
        parser.add_argument("--channel", required=True, choices=(API_MARKET_CHANNEL, ZYLA_CHANNEL))
        parser.add_argument("--product", required=True)

    def handle(self, *args, **options):
        try:
            row, raw_key = issue_marketplace_backend_key(
                channel=options["channel"],
                product_slug=options["product"],
            )
        except (KeyError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.WARNING("Store this value directly in the marketplace's private backend-auth configuration. It is shown once."))
        self.stdout.write(f"channel={options['channel']}")
        self.stdout.write(f"product={options['product']}")
        self.stdout.write(f"api_key_id={row.id}")
        self.stdout.write(f"api_key={raw_key}")
