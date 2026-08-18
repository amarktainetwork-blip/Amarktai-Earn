from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from control.services.api_distribution import (
    api_distribution_snapshot,
    api_market_export,
    bootstrap_api_distribution,
    postman_export,
    zyla_export,
)
from control.services.api_distribution_packages import enrich_distribution_packages
from control.services.commercial_api import openapi_spec
from control.services.commercial_intelligence import bootstrap_commercial_packages


class Command(BaseCommand):
    help = "Generate safe owner-uploadable API marketplace packages without publishing or performing external mutations."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", required=True)

    def handle(self, *args, **options):
        target = Path(options["output_dir"]).expanduser().resolve()
        if target.exists() and not target.is_dir():
            raise CommandError("output path exists and is not a directory")
        target.mkdir(parents=True, exist_ok=True)

        bootstrap_api_distribution()
        bootstrap_commercial_packages()
        enrichment = enrich_distribution_packages()

        artifacts = {
            "openapi.json": openapi_spec(),
            "api-market.json": api_market_export(),
            "zyla-api-hub.json": zyla_export(),
            "postman-collection.json": postman_export()["collection"],
            "distribution-snapshot.json": api_distribution_snapshot(),
        }
        for filename, payload in artifacts.items():
            path = target / filename
            path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

        serialized = "\n".join((target / name).read_text(encoding="utf-8") for name in artifacts)
        if "Bearer ak_" in serialized or '"api_key": "ak_' in serialized:
            raise CommandError("secret material detected in generated distribution package")

        self.stdout.write(self.style.SUCCESS(
            f"api distribution packages ready: output={target} files={len(artifacts)} enrichment={enrichment} external_publication=NO"
        ))
