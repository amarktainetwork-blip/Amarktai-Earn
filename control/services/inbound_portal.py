from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from django.core import signing
from django.db import transaction

from control.models import AuditEvent, InboundOrder
from control.services.channel_ingress import missing_buyer_inputs, refresh_order_after_intake
from planning.asset_policy import safe_asset_name
from planning.services import stage_local_job_asset


INTAKE_SALT = "amarktai-earn-inbound-intake-v1"
FILE_INPUTS = {"dataset", "source_data", "csv_input", "tabular_input", "image"}
EDITABLE_ORDER_STATES = {
    InboundOrder.Status.RECEIVED,
    InboundOrder.Status.PREFLIGHT_BLOCKED,
    InboundOrder.Status.READY,
}


class IntakePortalError(ValueError):
    pass


def _max_age() -> int:
    return max(300, min(int(os.getenv("INBOUND_INTAKE_TOKEN_SECONDS", str(7 * 24 * 3600))), 30 * 24 * 3600))


def _manifest(order: InboundOrder) -> dict:
    if not order.listing_id:
        return {}
    metadata = order.listing.platform_metadata if isinstance(order.listing.platform_metadata, dict) else {}
    manifest = metadata.get("channel_package")
    return manifest if isinstance(manifest, dict) else {}


def issue_intake_token(order: InboundOrder, *, actor: str = "owner") -> str:
    order = InboundOrder.objects.select_related("listing__offering", "marketplace").get(pk=order.pk)
    if order.status not in EDITABLE_ORDER_STATES:
        raise IntakePortalError("ORDER_NO_LONGER_ACCEPTS_BUYER_INPUT")
    token = signing.dumps(
        {
            "order_id": str(order.id),
            "market": order.marketplace.slug,
            "package_slug": order.listing.offering.slug if order.listing_id else "",
        },
        salt=INTAKE_SALT,
        compress=True,
    )
    AuditEvent.objects.create(
        event_type="inbound.intake_link_issued",
        actor=str(actor)[:120],
        metadata={"order_id": str(order.id), "market": order.marketplace.slug, "expires_in": _max_age()},
    )
    return token


def resolve_intake_token(token: str) -> InboundOrder:
    try:
        payload = signing.loads(str(token or ""), salt=INTAKE_SALT, max_age=_max_age())
    except signing.SignatureExpired as exc:
        raise IntakePortalError("INTAKE_LINK_EXPIRED") from exc
    except signing.BadSignature as exc:
        raise IntakePortalError("INTAKE_LINK_INVALID") from exc
    try:
        order = InboundOrder.objects.select_related("listing__offering", "marketplace", "job").get(pk=payload.get("order_id"))
    except InboundOrder.DoesNotExist as exc:
        raise IntakePortalError("INTAKE_ORDER_NOT_FOUND") from exc
    if payload.get("market") != order.marketplace.slug:
        raise IntakePortalError("INTAKE_LINK_MARKET_MISMATCH")
    package_slug = order.listing.offering.slug if order.listing_id else ""
    if payload.get("package_slug") != package_slug:
        raise IntakePortalError("INTAKE_LINK_PACKAGE_MISMATCH")
    return order


def buyer_input_spec(order: InboundOrder) -> list[dict]:
    manifest = _manifest(order)
    requirements = order.requirements if isinstance(order.requirements, dict) else {}
    has_assets = bool(order.input_assets)
    result = []
    for key in [str(item) for item in (manifest.get("buyer_inputs") or [])]:
        file_input = key in FILE_INPUTS
        result.append({
            "key": key,
            "label": key.replace("_", " ").title(),
            "type": "file" if file_input else "textarea",
            "required": True,
            "complete": has_assets if file_input else requirements.get(key) not in (None, "", [], {}),
            "value": "" if file_input else str(requirements.get(key) or ""),
        })
    return result


def intake_snapshot(order: InboundOrder) -> dict:
    order = InboundOrder.objects.select_related("listing__offering", "marketplace", "job").get(pk=order.pk)
    inputs = buyer_input_spec(order)
    return {
        "order_id": str(order.id),
        "market": order.marketplace.slug,
        "package_slug": order.listing.offering.slug if order.listing_id else None,
        "package_name": order.listing.offering.display_name if order.listing_id else "Order intake",
        "status": order.status,
        "remote_state": order.remote_state,
        "input_fields": inputs,
        "missing_inputs": missing_buyer_inputs(order),
        "editable": order.status in EDITABLE_ORDER_STATES,
    }


def _job_intake_dir(order: InboundOrder) -> Path:
    root = Path(os.getenv("AMARKTAI_JOB_ROOT", "/var/lib/amarktai-earn/jobs")).resolve()
    target = (root / str(order.job_id) / "buyer-intake").resolve()
    if target != root and root not in target.parents:
        raise IntakePortalError("INTAKE_STORAGE_PATH_ESCAPE")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write_upload(order: InboundOrder, uploaded, *, index: int) -> dict:
    maximum = max(1, int(os.getenv("JOB_ASSET_MAX_FILE_BYTES", str(100 * 1024 * 1024))))
    size = int(getattr(uploaded, "size", 0) or 0)
    if size <= 0 or size > maximum:
        raise IntakePortalError("INTAKE_FILE_SIZE_INVALID")
    original_name = safe_asset_name(str(getattr(uploaded, "name", "source.bin") or "source.bin"))
    destination = _job_intake_dir(order) / f"{index:02d}-{original_name}"
    written = 0
    with destination.open("wb") as handle:
        for chunk in uploaded.chunks():
            written += len(chunk)
            if written > maximum:
                handle.close()
                destination.unlink(missing_ok=True)
                raise IntakePortalError("INTAKE_FILE_SIZE_INVALID")
            handle.write(chunk)
    try:
        asset = stage_local_job_asset(
            job_id=order.job_id,
            path=str(destination),
            source=f"buyer-intake:{order.marketplace.slug}",
            external_id=f"buyer-intake:{order.id}:{index}",
            semantic_role="source",
            declared_mime_type=str(getattr(uploaded, "content_type", "") or "")[:120],
        )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return {
        "asset_id": str(asset.id),
        "sha256": asset.sha256,
        "size_bytes": asset.size_bytes,
        "name": asset.name,
        "semantic_role": asset.semantic_role,
    }


@transaction.atomic
def submit_buyer_intake(
    order: InboundOrder,
    *,
    fields: dict,
    uploads: Iterable,
    actor: str = "buyer-intake",
) -> InboundOrder:
    order = (
        InboundOrder.objects
        .select_related("listing__offering", "marketplace", "job")
        .select_for_update(of=("self",))
        .get(pk=order.pk)
    )
    if order.status not in EDITABLE_ORDER_STATES:
        raise IntakePortalError("ORDER_NO_LONGER_ACCEPTS_BUYER_INPUT")
    spec = buyer_input_spec(order)
    allowed_text = {row["key"] for row in spec if row["type"] != "file"}
    requirements = dict(order.requirements or {})
    for key in allowed_text:
        if key in fields:
            value = str(fields.get(key) or "").strip()
            if len(value.encode()) > max(1024, int(os.getenv("INBOUND_INTAKE_MAX_FIELD_BYTES", "32768"))):
                raise IntakePortalError("INTAKE_FIELD_TOO_LARGE")
            if value:
                requirements[key] = value

    upload_list = [item for item in uploads if item is not None]
    max_files = max(1, int(os.getenv("JOB_ASSET_MAX_FILES", "12")))
    if len(upload_list) > max_files:
        raise IntakePortalError("INTAKE_FILE_COUNT_EXCEEDED")
    assets = list(order.input_assets or [])
    start_index = len(assets) + 1
    for offset, uploaded in enumerate(upload_list):
        assets.append(_write_upload(order, uploaded, index=start_index + offset))

    order.requirements = requirements
    order.input_assets = assets
    order.save(update_fields=["requirements", "input_assets", "updated_at"])
    order = refresh_order_after_intake(order.id)
    AuditEvent.objects.create(
        event_type="inbound.buyer_intake_updated",
        actor=str(actor)[:120],
        metadata={
            "order_id": str(order.id),
            "market": order.marketplace.slug,
            "missing_inputs": missing_buyer_inputs(order),
            "file_count": len(order.input_assets or []),
        },
    )
    return order
