from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from control.models import (
    Alert,
    AuditEvent,
    GenXAccountSnapshot,
    GenXCall,
    GenXModelCatalog,
    Job,
    JobScore,
    ModelStat,
    Worker,
)
from gateways.genx.client import GenXClient, GenXError
from gateways.genx.output import extract_text
from gateways.genx.contracts import (
    ModelCandidate,
    assert_credit_budget,
    available_credits,
    effective_reserved_credits,
    model_id,
    price_hint,
    pricing_index,
    rank_models,
    records,
    result_url,
    usage_credits,
)


class GenXGatewayError(RuntimeError):
    pass


class GenXBudgetExceeded(GenXGatewayError):
    pass


class GenXModelUnavailable(GenXGatewayError):
    pass


def configured_client() -> GenXClient:
    return GenXClient(
        api_key=os.getenv("GENX_API_KEY", ""),
        base_url=os.getenv("GENX_BASE_URL", "https://query.genx.sh"),
        timeout=int(os.getenv("GENX_TIMEOUT_SECONDS", "30")),
    )


class GenXGateway:
    """Controller-owned GenX gateway with catalog sync, routing, reservation, and persisted usage."""

    def __init__(self, client: GenXClient | None = None):
        self.client = client or configured_client()

    def sync_catalog(self, category: str | None = None) -> dict[str, Any]:
        # Never hold a database transaction open while waiting on a third-party API.
        models_payload = self.client.list_models(category)
        pricing_payload = self.client.pricing(category)
        credits_payload = self.client.credits()
        pricing_by_model = pricing_index(pricing_payload)
        seen: set[str] = set()
        now = timezone.now()

        with transaction.atomic():
            for row in records(models_payload):
                mid = model_id(row)
                if not mid:
                    continue
                seen.add(mid)
                pricing_row = pricing_by_model.get(mid, {})
                GenXModelCatalog.objects.update_or_create(
                    model_id=mid,
                    defaults={
                        "category": str(row.get("category") or category or "")[:40],
                        "provider": str(row.get("provider") or row.get("vendor") or "")[:120],
                        "active": True,
                        "price_hint": price_hint(pricing_row),
                        "model_payload": row,
                        "pricing_payload": pricing_row,
                        "last_seen_at": now,
                    },
                )

            stale = GenXModelCatalog.objects.filter(active=True)
            if category:
                stale = stale.filter(category=category)
            if seen:
                stale.exclude(model_id__in=seen).update(active=False)

            credits = available_credits(credits_payload)
            GenXAccountSnapshot.objects.create(available_credits=credits, raw=credits_payload)
            AuditEvent.objects.create(
                event_type="genx.catalog_synced",
                actor="genx-gateway",
                metadata={"category": category or "all", "models_seen": len(seen), "credits_available": str(credits) if credits is not None else None},
            )
        return {"models_seen": len(seen), "available_credits": credits}

    def select_model(self, *, task_class: str, category: str, preferred_model: str | None = None) -> GenXModelCatalog:
        catalog = GenXModelCatalog.objects.filter(active=True, category=category)
        if preferred_model:
            selected = catalog.filter(model_id=preferred_model).first()
            if not selected:
                raise GenXModelUnavailable(f"preferred model {preferred_model!r} is not active for category {category!r}")
            return selected

        rows = list(catalog)
        if not rows:
            raise GenXModelUnavailable(f"no active GenX models are cached for category {category!r}; sync catalog first")
        stats = {item.model: item for item in ModelStat.objects.filter(task_class=task_class, model__in=[row.model_id for row in rows])}
        candidates = []
        by_id = {row.model_id: row for row in rows}
        for row in rows:
            stat = stats.get(row.model_id)
            candidates.append(
                ModelCandidate(
                    model_id=row.model_id,
                    price_hint=row.price_hint,
                    attempts=stat.attempts if stat else 0,
                    accepted=stat.accepted if stat else 0,
                    profit=stat.profit if stat else Decimal("0"),
                    credits=stat.credits if stat else Decimal("0"),
                )
            )
        return by_id[rank_models(candidates)[0].model_id]

    @transaction.atomic
    def _reserve_call(
        self,
        *,
        job_id,
        worker_id: str,
        model: str,
        task_class: str,
        estimated_credits: Decimal,
        max_allowed_credits: Decimal,
        request_key: str | None,
        metadata: dict[str, Any],
    ) -> tuple[GenXCall, bool]:
        job = Job.objects.select_for_update().select_related("marketplace").get(pk=job_id)
        score = JobScore.objects.select_for_update().get(job=job)
        worker = Worker.objects.select_for_update().get(pk=worker_id)
        if request_key:
            existing = GenXCall.objects.filter(request_key=request_key).first()
            if existing:
                return existing, False

        reserved = effective_reserved_credits(
            GenXCall.objects.filter(job=job).values_list("credits", "estimated_credits", "status")
        )
        try:
            assert_credit_budget(
                already_reserved=reserved,
                estimated=estimated_credits,
                call_limit=max_allowed_credits,
                job_limit=score.max_genx_credits,
            )
        except ValueError as exc:
            raise GenXBudgetExceeded(str(exc)) from exc

        call = GenXCall.objects.create(
            request_key=request_key,
            job=job,
            worker=worker,
            model=model,
            task_class=task_class,
            marketplace_slug=job.marketplace.slug,
            estimated_credits=estimated_credits,
            max_allowed_credits=max_allowed_credits,
            status="RESERVED",
            requested_metadata=metadata,
            started_at=timezone.now(),
        )
        AuditEvent.objects.create(
            event_type="genx.call_reserved",
            actor="genx-gateway",
            metadata={
                "call_id": str(call.id),
                "job_id": str(job.id),
                "worker": worker.id,
                "model": model,
                "estimated_credits": str(estimated_credits),
                "job_budget_credits": str(score.max_genx_credits),
            },
        )
        return call, True

    def run(
        self,
        *,
        job_id,
        worker_id: str,
        category: str,
        task_class: str,
        params: dict[str, Any],
        estimated_credits: Decimal,
        max_allowed_credits: Decimal,
        request_key: str | None = None,
        preferred_model: str | None = None,
        wait_timeout_seconds: int = 180,
    ) -> GenXCall:
        selected = self.select_model(task_class=task_class, category=category, preferred_model=preferred_model)
        job = Job.objects.select_related("marketplace").get(pk=job_id)
        metadata = {
            "job": str(job.id),
            "worker": worker_id,
            "marketplace": job.marketplace.slug,
            "task_class": task_class,
            "model_requested": selected.model_id,
            "max_credits": str(max_allowed_credits),
        }
        call, created = self._reserve_call(
            job_id=job.id,
            worker_id=worker_id,
            model=selected.model_id,
            task_class=task_class,
            estimated_credits=Decimal(estimated_credits),
            max_allowed_credits=Decimal(max_allowed_credits),
            request_key=request_key,
            metadata=metadata,
        )
        if not created:
            # Never replay a request key automatically. A RESERVED/SUBMITTING call may
            # represent a process crash after the remote request was accepted.
            return call

        started = time.monotonic()
        try:
            GenXCall.objects.filter(pk=call.pk).update(status="SUBMITTING")
            submission = self.client.generate(selected.model_id, params, metadata=metadata)
            external_job_id = str(submission.get("job_id") or submission.get("id") or "")
            if not external_job_id:
                raise GenXGatewayError("GenX generation response did not contain a job ID")
            GenXCall.objects.filter(pk=call.pk).update(external_job_id=external_job_id, status="SUBMITTED")
            final = self.client.wait(external_job_id, timeout_seconds=wait_timeout_seconds)
            return self.reconcile(call.id, final, elapsed_ms=int((time.monotonic() - started) * 1000))
        except (GenXError, GenXGatewayError, TimeoutError) as exc:
            # A timeout/network/server error after submission is not proof that the
            # remote asynchronous job failed. Preserve the reservation and require
            # reconciliation instead of enabling an unsafe replay. Only a confirmed
            # 4xx rejection before a remote job id exists is treated as FAILED.
            confirmed_rejection = (
                isinstance(exc, GenXError)
                and exc.status_code is not None
                and 400 <= exc.status_code < 500
                and exc.status_code != 429
                and not GenXCall.objects.filter(pk=call.pk).exclude(external_job_id="").exists()
            )
            next_status = "FAILED" if confirmed_rejection else "UNKNOWN_REMOTE_STATE"
            GenXCall.objects.filter(pk=call.pk).update(
                status=next_status,
                latency_ms=int((time.monotonic() - started) * 1000),
                completed_at=timezone.now() if confirmed_rejection else None,
                error_code=exc.__class__.__name__,
            )
            AuditEvent.objects.create(
                severity="ERROR" if confirmed_rejection else "WARNING",
                event_type="genx.call_failed" if confirmed_rejection else "genx.call_unknown_remote_state",
                actor="genx-gateway",
                metadata={"call_id": str(call.id), "job_id": str(job.id), "error_code": exc.__class__.__name__},
            )
            raise

    def run_session(
        self,
        *,
        job_id,
        worker_id: str,
        task_class: str,
        system_prompt: str,
        message: str,
        estimated_credits: Decimal,
        max_allowed_credits: Decimal,
        request_key: str,
        tools: list | None = None,
        preferred_model: str | None = None,
    ) -> tuple[GenXCall, dict[str, Any]]:
        """Run one documented GenX stateful session message with controller-side budget truth."""
        selected = self.select_model(task_class=task_class, category="text", preferred_model=preferred_model)
        job = Job.objects.select_related("marketplace").get(pk=job_id)
        metadata = {
            "job": str(job.id),
            "worker": worker_id,
            "marketplace": job.marketplace.slug,
            "task_class": task_class,
            "model_requested": selected.model_id,
            "transport": "session",
            "max_credits": str(max_allowed_credits),
        }
        call, created = self._reserve_call(
            job_id=job.id,
            worker_id=worker_id,
            model=selected.model_id,
            task_class=task_class,
            estimated_credits=Decimal(estimated_credits),
            max_allowed_credits=Decimal(max_allowed_credits),
            request_key=request_key,
            metadata=metadata,
        )
        if not created:
            if call.external_job_id.startswith("session:"):
                session_id = call.external_job_id.split(":", 1)[1]
                return call, self.client.session_messages(session_id)
            return call, {}

        started = time.monotonic()
        session_id = ""
        try:
            GenXCall.objects.filter(pk=call.pk).update(status="SUBMITTING")
            session = self.client.create_session(
                selected.model_id,
                system_prompt=system_prompt,
                title=f"Amarktai Earn {task_class} {str(job.id)[:8]}",
            )
            session_id = str(session.get("session_id") or session.get("id") or "")
            if not session_id:
                raise GenXGatewayError("GenX session response did not contain a session ID")
            GenXCall.objects.filter(pk=call.pk).update(external_job_id=f"session:{session_id}", status="SUBMITTED")
            response = self.client.session_message(
                session_id,
                message,
                idempotency_key=request_key,
                tools=tools,
            )
            reconciled = self.reconcile(
                call.id,
                {
                    "status": "COMPLETED",
                    "usage": response.get("usage") if isinstance(response.get("usage"), dict) else {},
                    "billing": response.get("billing") if isinstance(response.get("billing"), dict) else {},
                },
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            try:
                self.client.close_session(session_id)
            except GenXError:
                AuditEvent.objects.create(
                    severity="WARNING",
                    event_type="genx.session_close_failed",
                    actor="genx-gateway",
                    metadata={"call_id": str(call.id), "session_id": session_id},
                )
            return reconciled, response
        except (GenXError, GenXGatewayError, TimeoutError) as exc:
            confirmed_rejection = (
                isinstance(exc, GenXError)
                and exc.status_code is not None
                and 400 <= exc.status_code < 500
                and exc.status_code != 429
                and not session_id
            )
            next_status = "FAILED" if confirmed_rejection else "UNKNOWN_REMOTE_STATE"
            GenXCall.objects.filter(pk=call.pk).update(
                status=next_status,
                latency_ms=int((time.monotonic() - started) * 1000),
                completed_at=timezone.now() if confirmed_rejection else None,
                error_code=exc.__class__.__name__,
            )
            AuditEvent.objects.create(
                severity="ERROR" if confirmed_rejection else "WARNING",
                event_type="genx.session_failed" if confirmed_rejection else "genx.session_unknown_remote_state",
                actor="genx-gateway",
                metadata={"call_id": str(call.id), "job_id": str(job.id), "error_code": exc.__class__.__name__},
            )
            raise

    def reconcile_pending(self, limit: int = 100) -> dict[str, int]:
        reconciled = 0
        unresolved = 0
        calls = list(
            GenXCall.objects.filter(status__in=["SUBMITTED", "UNKNOWN_REMOTE_STATE"])
            .exclude(external_job_id="")
            .order_by("created_at")[:limit]
        )
        for call in calls:
            try:
                if call.external_job_id.startswith("session:"):
                    session_id = call.external_job_id.split(":", 1)[1]
                    payload = self.client.session_messages(session_id)
                    if not extract_text(payload):
                        unresolved += 1
                        continue
                    self.reconcile(
                        call.id,
                        {
                            "status": "COMPLETED",
                            "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
                            "billing": payload.get("billing") if isinstance(payload.get("billing"), dict) else {},
                        },
                    )
                else:
                    payload = self.client.job(call.external_job_id)
                    self.reconcile(call.id, payload)
                reconciled += 1
            except GenXError:
                unresolved += 1
        return {"reconciled": reconciled, "unresolved": unresolved}

    @transaction.atomic
    def reconcile(self, call_id, payload: dict[str, Any], elapsed_ms: int = 0) -> GenXCall:
        call = GenXCall.objects.select_for_update().select_related("job").get(pk=call_id)
        was_terminal = call.status in {"COMPLETED", "FAILED", "CANCELLED"} and call.completed_at is not None
        status = str(payload.get("status") or "UNKNOWN").upper()
        actual_credits = usage_credits(payload) or Decimal("0")
        call.status = status
        call.credits = actual_credits
        call.usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        call.result_url = result_url(payload)
        call.latency_ms = max(0, elapsed_ms)
        call.completed_at = timezone.now() if status in {"COMPLETED", "FAILED", "CANCELLED"} else None
        call.error_code = str(payload.get("error_code") or "")[:120]
        call.save(
            update_fields=["status", "credits", "usage", "result_url", "latency_ms", "completed_at", "error_code", "updated_at"]
        )

        if not was_terminal:
            stat, _ = ModelStat.objects.select_for_update().get_or_create(model=call.model, task_class=call.task_class)
            stat.attempts += 1
            stat.credits += actual_credits
            stat.total_latency_ms += call.latency_ms
            stat.save(update_fields=["attempts", "credits", "total_latency_ms", "updated_at"])

        if not was_terminal and status == "COMPLETED" and actual_credits == 0:
            Alert.objects.create(
                severity="WARNING",
                alert_type="GENX_USAGE_MISSING",
                message="Completed GenX call returned no parseable credit usage; cost truth requires reconciliation.",
                metadata={"call_id": str(call.id), "external_job_id": call.external_job_id},
            )
        if not was_terminal and actual_credits > call.max_allowed_credits:
            Alert.objects.create(
                severity="CRITICAL",
                alert_type="GENX_CALL_BUDGET_OVERRUN",
                message="A completed GenX call exceeded its controller-side credit estimate ceiling.",
                metadata={"call_id": str(call.id), "actual": str(actual_credits), "limit": str(call.max_allowed_credits)},
            )
        if call.job_id:
            total = effective_reserved_credits(
                GenXCall.objects.filter(job_id=call.job_id).values_list("credits", "estimated_credits", "status")
            )
            score = call.job.jobscore
            if not was_terminal and score.max_genx_credits > 0 and total > score.max_genx_credits:
                Alert.objects.create(
                    severity="CRITICAL",
                    alert_type="GENX_JOB_BUDGET_OVERRUN",
                    message="A job exceeded its total GenX credit budget.",
                    metadata={"job_id": str(call.job_id), "reserved_or_charged": str(total), "limit": str(score.max_genx_credits)},
                )

        AuditEvent.objects.create(
            event_type="genx.call_reconciled",
            actor="genx-gateway",
            metadata={
                "call_id": str(call.id),
                "status": status,
                "credits": str(actual_credits),
                "model": call.model,
                "latency_ms": call.latency_ms,
            },
        )
        return call
