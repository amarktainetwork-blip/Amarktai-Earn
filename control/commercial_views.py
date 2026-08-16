from __future__ import annotations

import json
import uuid

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from control.models import CommercialAPIProduct, CommercialAPIRequest
from control.services.commercial_api import (
    CommercialAPIError,
    admit_request,
    authenticate_request,
    execute_request,
    openapi_spec,
    request_payload,
)
from control.services.commercial_intelligence import record_conversion_event


def _error(exc: CommercialAPIError, request_id: str) -> JsonResponse:
    response = JsonResponse({"error": {"code": exc.code, "message": exc.detail}, "request_id": request_id}, status=exc.status)
    if exc.status == 429:
        response["Retry-After"] = "60"
    return response


def _json_body(request, *, max_bytes: int = 65536) -> dict:
    if len(request.body) > max_bytes:
        raise CommercialAPIError("REQUEST_TOO_LARGE", status=413)
    if not request.body:
        return {}
    try:
        value = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommercialAPIError("INVALID_SCHEMA") from exc
    if not isinstance(value, dict):
        raise CommercialAPIError("INVALID_SCHEMA")
    return value


@require_GET
def api_docs_page(request):
    products = CommercialAPIProduct.objects.filter(enabled=True).prefetch_related("plans").order_by("slug")
    return render(request, "control/api_docs.html", {"products": products})


@require_GET
def openapi_json(request):
    return JsonResponse(openapi_spec(request))


@csrf_exempt
@require_POST
def commercial_api_submit(request, product_slug: str):
    correlation_id = (request.headers.get("X-Request-ID") or str(uuid.uuid4()))[:64]
    try:
        product = CommercialAPIProduct.objects.select_related("offering").filter(slug=product_slug, enabled=True).first()
        if product is None:
            raise CommercialAPIError("UNKNOWN_API_PRODUCT", status=404)
        identity = authenticate_request(request, product)
        payload = _json_body(request, max_bytes=int((product.request_limits or {}).get("max_bytes") or 262144))
        row, created = admit_request(
            identity=identity,
            product=product,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            payload=payload,
            correlation_id=correlation_id,
        )
        if created and product.execution_class == CommercialAPIProduct.ExecutionClass.SYNCHRONOUS:
            row = execute_request(row)
        elif created:
            row.status = CommercialAPIRequest.Status.QUEUED
            row.save(update_fields=["status", "updated_at"])
            from control.queueing import queue, rq_job_id
            from control.tasks import execute_commercial_api_request_task

            target = queue("p3")
            try:
                target.enqueue(
                    execute_commercial_api_request_task,
                    str(row.id),
                    job_id=rq_job_id("commercial-api", product.slug, row.id),
                    result_ttl=86400,
                    failure_ttl=604800,
                )
            except Exception as exc:
                row.status = CommercialAPIRequest.Status.FAILED
                row.error_code = "EXECUTION_FAILED"
                row.error_detail = "The execution queue is temporarily unavailable."
                row.save(update_fields=["status", "error_code", "error_detail", "updated_at"])
                raise CommercialAPIError("EXECUTION_FAILED", status=503, detail=row.error_detail) from exc
        if row.status == CommercialAPIRequest.Status.FAILED:
            return _error(CommercialAPIError(row.error_code or "EXECUTION_FAILED", status=503, detail=row.error_detail or "The canonical execution failed safely."), row.correlation_id)
        status = 200 if row.status == CommercialAPIRequest.Status.COMPLETED else 202
        response = JsonResponse(request_payload(row, include_result=status == 200), status=status)
        response["X-Request-ID"] = row.correlation_id
        response["Idempotent-Replay"] = "false" if created else "true"
        return response
    except CommercialAPIError as exc:
        return _error(exc, correlation_id)
    except Exception:  # noqa: BLE001 - the public boundary must never leak worker exceptions
        return _error(CommercialAPIError("EXECUTION_FAILED", status=503), correlation_id)


def _request_for_key(request, request_id: str) -> CommercialAPIRequest:
    row = CommercialAPIRequest.objects.select_related("product", "api_key__plan").filter(pk=request_id).first()
    if row is None:
        raise CommercialAPIError("UNKNOWN_API_PRODUCT", status=404, detail="The request does not exist for this API key.")
    identity = authenticate_request(request, row.product)
    if row.api_key_id != identity.key.id:
        raise CommercialAPIError("UNKNOWN_API_PRODUCT", status=404, detail="The request does not exist for this API key.")
    return row


@require_GET
def commercial_api_status(request, request_id: str):
    correlation_id = (request.headers.get("X-Request-ID") or str(uuid.uuid4()))[:64]
    try:
        return JsonResponse(request_payload(_request_for_key(request, request_id)))
    except CommercialAPIError as exc:
        return _error(exc, correlation_id)


@require_GET
def commercial_api_result(request, request_id: str):
    correlation_id = (request.headers.get("X-Request-ID") or str(uuid.uuid4()))[:64]
    try:
        row = _request_for_key(request, request_id)
        if row.status != CommercialAPIRequest.Status.COMPLETED or not row.qa_passed:
            return JsonResponse({"error": {"code": "RESULT_NOT_READY", "message": "A result is released only after canonical QA passes."}, "request_id": str(row.id), "status": row.status}, status=409)
        return JsonResponse(request_payload(row, include_result=True))
    except CommercialAPIError as exc:
        return _error(exc, correlation_id)


@csrf_exempt
@require_POST
def public_conversion_event(request):
    correlation_id = (request.headers.get("X-Request-ID") or str(uuid.uuid4()))[:64]
    try:
        payload = _json_body(request)
        row, created = record_conversion_event(
            event_id=str(payload.get("event_id") or correlation_id),
            event_type=str(payload.get("event_type") or ""),
            anonymous_reference=str(payload.get("anonymous_session") or correlation_id),
            product_slug=str(payload.get("product") or ""),
            variant_id=payload.get("variant_id"),
            source=str(payload.get("source") or "public-site"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )
        return JsonResponse({"accepted": True, "created": created, "event_id": row.event_id}, status=202)
    except ValueError as exc:
        return JsonResponse({"error": {"code": str(exc), "message": "The telemetry event was rejected safely."}, "request_id": correlation_id}, status=400)
