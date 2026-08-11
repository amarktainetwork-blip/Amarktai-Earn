from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Q
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
from control.services.admission import AdmissionDenied, require_admission
from control.services.genx_valuation import current_credit_valuation, monetary_cost_for_credits
from gateways.genx.client import GenXClient, GenXError
from gateways.genx.contracts import (
    ModelCandidate,
    assert_credit_budget,
    available_credits,
    effective_reserved_credits,
    model_id,
    price_hint,
    pricing_credit_estimate,
    pricing_index,
    records,
    result_url,
    route_models,
    usage_credits,
)
from gateways.genx.output import (
    decode_text_result_url,
    extract_session_assistant_text,
    session_assistant_job_ids,
)


ZERO = Decimal("0")


class GenXGatewayError(RuntimeError):
    pass


class GenXBudgetExceeded(GenXGatewayError):
    pass


class GenXModelUnavailable(GenXGatewayError):
    pass


class GenXMonetaryValuationUnavailable(GenXModelUnavailable):
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
                        "category": str(
                            row.get("category") or pricing_row.get("category") or category or ""
                        )[:40],
                        "provider": str(
                            row.get("provider")
                            or row.get("vendor")
                            or pricing_row.get("provider")
                            or pricing_row.get("vendor")
                            or ""
                        )[:120],
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

    def select_model(
        self,
        *,
        task_class: str,
        category: str,
        preferred_model: str | None = None,
        preferred_override_reason: str = "",
        eligible_model_ids: list[str] | tuple[str, ...] | None = None,
        required_quality: Decimal = Decimal("0.80"),
        expected_revenue: Decimal = Decimal("0"),
        non_genx_cost: Decimal = Decimal("0"),
        max_genx_credits: Decimal | None = None,
        estimated_credits: Decimal = Decimal("0.25"),
        params: dict[str, Any] | None = None,
        accounting_currency: str = "USD",
        allow_exploration: bool = False,
        economically_fragile: bool = False,
    ) -> GenXModelCatalog:
        catalog = GenXModelCatalog.objects.filter(active=True, category=category)
        if eligible_model_ids is not None:
            eligible = tuple(dict.fromkeys(str(value) for value in eligible_model_ids if value))
            if not eligible:
                raise GenXModelUnavailable(f"no active GenX models satisfy the required capability for {task_class!r}")
            catalog = catalog.filter(model_id__in=eligible)
        if preferred_model:
            allowed_reasons = {"ADMIN_PROOF", "DEBUG_PROOF", "CONTROLLER_SIGNED_ECONOMIC_SELECTION"}
            if preferred_override_reason not in allowed_reasons:
                raise GenXModelUnavailable("explicit GenX model override is restricted to controlled proof/debug boundaries")
            selected = catalog.filter(model_id=preferred_model).first()
            if not selected:
                raise GenXModelUnavailable(f"preferred model {preferred_model!r} is not active for category {category!r}")
            AuditEvent.objects.create(
                event_type="genx.explicit_model_override",
                actor="genx-router",
                metadata={"task_class": task_class, "model": selected.model_id, "reason": preferred_override_reason},
            )
            return selected

        rows = list(catalog)
        if not rows:
            raise GenXModelUnavailable(f"no active GenX models are cached for category {category!r}; sync catalog first")
        stats = {item.model: item for item in ModelStat.objects.filter(task_class=task_class, model__in=[row.model_id for row in rows])}
        candidates = []
        by_id = {row.model_id: row for row in rows}
        for row in rows:
            stat = stats.get(row.model_id)
            historical_average = None
            if stat and stat.attempts and stat.credits > 0:
                historical_average = stat.credits / Decimal(stat.attempts)
            candidates.append(
                ModelCandidate(
                    model_id=row.model_id,
                    price_hint=row.price_hint,
                    expected_credits=pricing_credit_estimate(
                        row.pricing_payload,
                        params,
                        historical_average=historical_average,
                        reserved_envelope=estimated_credits,
                    ),
                    attempts=stat.attempts if stat else 0,
                    accepted=stat.accepted if stat else 0,
                    profit=stat.profit if stat else Decimal("0"),
                    credits=stat.credits if stat else Decimal("0"),
                    successful_executions=stat.successful_executions if stat else 0,
                    qa_accepted=stat.qa_accepted if stat else 0,
                    qa_rejected=stat.qa_rejected if stat else 0,
                    repair_required=stat.repair_required if stat else 0,
                    failures=stat.failures if stat else 0,
                    provider_failures=stat.provider_failures if stat else 0,
                    retry_count=stat.retry_count if stat else 0,
                    total_repair_cost=stat.total_repair_cost if stat else Decimal("0"),
                    net_profit=stat.net_profit if stat else Decimal("0"),
                )
            )
        valuation = current_credit_valuation(currency=accounting_currency)
        routed = route_models(
            candidates,
            expected_revenue=expected_revenue,
            non_genx_cost=non_genx_cost,
            required_quality=required_quality,
            max_genx_credits=max_genx_credits,
            monetary_cost_per_credit=valuation.monetary_cost_per_credit if valuation else None,
            allow_exploration=allow_exploration and not economically_fragile,
            exploration_fraction=Decimal(os.getenv("GENX_EXPLORATION_BUDGET_FRACTION", "0.05")),
        )
        if expected_revenue > 0 and valuation is None:
            AuditEvent.objects.create(
                severity="WARNING",
                event_type="genx.monetary_profit_unresolved",
                actor="genx-router",
                metadata={"task_class": task_class, "category": category, "currency": accounting_currency},
            )
            raise GenXMonetaryValuationUnavailable(
                f"no verified GenX credit valuation exists in {accounting_currency}; monetary profitability is unresolved"
            )
        if not routed:
            raise GenXModelUnavailable("no active model satisfies the task quality, credit-budget, and economic constraints")
        selected = routed[0]
        AuditEvent.objects.create(
            event_type="genx.economic_model_selected",
            actor="genx-router",
            metadata={
                "task_class": task_class,
                "category": category,
                "model": selected.candidate.model_id,
                "cost_basis": selected.cost_basis,
                "expected_net_profit": str(selected.expected_net_profit) if selected.expected_net_profit is not None else None,
                "non_currency_score": str(selected.non_currency_score),
                "expected_credits": str(selected.expected_credits),
                "expected_total_cost": str(selected.expected_total_cost) if selected.expected_total_cost is not None else None,
                "quality_probability": str(selected.quality_probability),
                "required_quality": str(required_quality),
                "valuation_version": valuation.version if valuation else None,
                "exploration": selected.exploration,
            },
        )
        return by_id[selected.candidate.model_id]

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
            GenXCall.objects.filter(job=job).values_list(
                "credits", "estimated_credits", "status", "requested_metadata"
            )
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
        preferred_override_reason: str = "",
        eligible_model_ids: list[str] | tuple[str, ...] | None = None,
        wait_timeout_seconds: int = 180,
        required_quality: Decimal = Decimal("0.80"),
        expected_revenue: Decimal | None = None,
        non_genx_cost: Decimal = Decimal("0"),
        allow_exploration: bool = False,
        economically_fragile: bool = False,
    ) -> GenXCall:
        job = Job.objects.select_related("marketplace").get(pk=job_id)
        selected = self.select_model(
            task_class=task_class,
            category=category,
            preferred_model=preferred_model,
            preferred_override_reason=preferred_override_reason,
            eligible_model_ids=eligible_model_ids,
            required_quality=required_quality,
            expected_revenue=expected_revenue if expected_revenue is not None else Decimal("0"),
            non_genx_cost=non_genx_cost,
            max_genx_credits=max_allowed_credits,
            estimated_credits=estimated_credits,
            params=params,
            accounting_currency=job.currency,
            allow_exploration=allow_exploration,
            economically_fragile=economically_fragile,
        )
        try:
            require_admission(purpose="GENX", job=job)
        except AdmissionDenied as exc:
            raise GenXBudgetExceeded(str(exc)) from exc
        metadata = {
            "job": str(job.id),
            "worker": worker_id,
            "marketplace": job.marketplace.slug,
            "task_class": task_class,
            "model_requested": selected.model_id,
            "max_credits": str(max_allowed_credits),
        }
        estimated_cost = monetary_cost_for_credits(
            credits=Decimal(estimated_credits), currency=job.currency, at=timezone.now()
        )
        metadata.update({
            "accounting_currency": job.currency,
            "cost_equivalent_truth": "ESTIMATED" if estimated_cost else "UNRESOLVED",
            "estimated_cost_equivalent": str(estimated_cost.amount) if estimated_cost else None,
            "valuation_version": estimated_cost.version if estimated_cost else None,
            "valuation_source": estimated_cost.source if estimated_cost else None,
            "valuation_id": estimated_cost.valuation_id if estimated_cost else None,
        })
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
            failure_metadata = dict(call.requested_metadata or metadata)
            if confirmed_rejection:
                failure_metadata.update({"billing_truth": "NOT_APPLICABLE", "cost_equivalent_truth": "NOT_APPLICABLE"})
            GenXCall.objects.filter(pk=call.pk).update(
                status=next_status,
                latency_ms=int((time.monotonic() - started) * 1000),
                completed_at=timezone.now() if confirmed_rejection else None,
                error_code=exc.__class__.__name__,
                cost_equivalent=ZERO if confirmed_rejection else None,
                requested_metadata=failure_metadata,
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
        preferred_override_reason: str = "",
        eligible_model_ids: list[str] | tuple[str, ...] | None = None,
        wait_timeout_seconds: int = 180,
        required_quality: Decimal = Decimal("0.80"),
        expected_revenue: Decimal | None = None,
        non_genx_cost: Decimal = Decimal("0"),
        allow_exploration: bool = False,
        economically_fragile: bool = False,
    ) -> tuple[GenXCall, dict[str, Any]]:
        """Submit one session message, then reconcile its asynchronous remote job."""
        job = Job.objects.select_related("marketplace").get(pk=job_id)
        selected = self.select_model(
            task_class=task_class,
            category="text",
            preferred_model=preferred_model,
            preferred_override_reason=preferred_override_reason,
            eligible_model_ids=eligible_model_ids,
            required_quality=required_quality,
            expected_revenue=expected_revenue if expected_revenue is not None else Decimal("0"),
            non_genx_cost=non_genx_cost,
            max_genx_credits=max_allowed_credits,
            estimated_credits=estimated_credits,
            params={"tools": tools or []},
            accounting_currency=job.currency,
            allow_exploration=allow_exploration,
            economically_fragile=economically_fragile,
        )
        try:
            require_admission(purpose="GENX", job=job)
        except AdmissionDenied as exc:
            raise GenXBudgetExceeded(str(exc)) from exc
        metadata = {
            "job": str(job.id),
            "worker": worker_id,
            "marketplace": job.marketplace.slug,
            "task_class": task_class,
            "model_requested": selected.model_id,
            "transport": "session",
            "max_credits": str(max_allowed_credits),
        }
        estimated_cost = monetary_cost_for_credits(
            credits=Decimal(estimated_credits), currency=job.currency, at=timezone.now()
        )
        metadata.update({
            "accounting_currency": job.currency,
            "cost_equivalent_truth": "ESTIMATED" if estimated_cost else "UNRESOLVED",
            "estimated_cost_equivalent": str(estimated_cost.amount) if estimated_cost else None,
            "valuation_version": estimated_cost.version if estimated_cost else None,
            "valuation_source": estimated_cost.source if estimated_cost else None,
            "valuation_id": estimated_cost.valuation_id if estimated_cost else None,
        })
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
            # A request key is a paid-POST boundary. Never replay session creation or
            # message submission, even if the prior process stopped mid-lifecycle.
            return call, {
                "assistant_text": str(call.requested_metadata.get("assistant_text") or ""),
                "result_url": call.result_url,
                "remote_job_id": str(call.requested_metadata.get("remote_job_id") or ""),
            }

        started = time.monotonic()
        session_id = ""
        message_id = ""
        remote_job_id = ""
        phase = "CREATE_SESSION"
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
            metadata = {
                **metadata,
                "session_id": session_id,
                "message_id": "",
                "remote_job_id": "",
                "billing_truth": "PENDING",
            }
            GenXCall.objects.filter(pk=call.pk).update(
                external_job_id=f"session:{session_id}",
                status="SUBMITTING",
                requested_metadata=metadata,
            )
            self._audit_session_phase("genx.session_created", call, metadata, status="CREATED")
            phase = "SEND_MESSAGE"
            response = self.client.session_message(
                session_id,
                message,
                idempotency_key=request_key,
                tools=tools,
            )
            remote_job_id = str(response.get("job_id") or "")
            message_id = str(response.get("message_id") or "")
            if not remote_job_id:
                raise GenXGatewayError("GenX session message acknowledgement did not contain a job ID")
            metadata = {
                **metadata,
                "message_id": message_id,
                "remote_job_id": remote_job_id,
                "billing_truth": "PENDING",
            }
            GenXCall.objects.filter(pk=call.pk).update(
                external_job_id=remote_job_id,
                status="SUBMITTED",
                requested_metadata=metadata,
            )
            self._audit_session_phase("genx.session_message_submitted", call, metadata, status="SUBMITTED")
            phase = "POLL_REMOTE_JOB"
            final = self.client.wait(remote_job_id, timeout_seconds=wait_timeout_seconds)
            if str(final.get("status") or "").lower() not in {"completed", "failed", "cancelled"}:
                raise GenXGatewayError("GenX wait returned a non-terminal remote job")
            reconciled = self.reconcile_remote_job_payload(
                call.id,
                final,
                source="POLL",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except (GenXError, GenXGatewayError, TimeoutError) as exc:
            create_rejection = (
                phase == "CREATE_SESSION"
                and isinstance(exc, GenXError)
                and exc.status_code is not None
                and 400 <= exc.status_code < 500
                and exc.status_code != 429
            )
            message_validation_rejection = (
                phase == "SEND_MESSAGE"
                and isinstance(exc, GenXError)
                and exc.status_code in {400, 422}
            )
            confirmed_rejection = create_rejection or message_validation_rejection
            next_status = "FAILED" if confirmed_rejection else "UNKNOWN_REMOTE_STATE"
            current = GenXCall.objects.get(pk=call.pk)
            failure_metadata = dict(current.requested_metadata or metadata)
            if confirmed_rejection:
                failure_metadata.update({"billing_truth": "NOT_APPLICABLE", "cost_equivalent_truth": "NOT_APPLICABLE"})
            GenXCall.objects.filter(pk=call.pk).update(
                status=next_status,
                latency_ms=int((time.monotonic() - started) * 1000),
                completed_at=timezone.now() if confirmed_rejection else None,
                error_code=exc.__class__.__name__,
                cost_equivalent=ZERO if confirmed_rejection else None,
                requested_metadata=failure_metadata,
            )
            AuditEvent.objects.create(
                severity="ERROR" if confirmed_rejection else "WARNING",
                event_type="genx.session_failed" if confirmed_rejection else "genx.session_unknown_remote_state",
                actor="genx-gateway",
                metadata={
                    "call_id": str(call.id),
                    "job_id": str(job.id),
                    "error_code": exc.__class__.__name__,
                    "phase": phase,
                    "http_status": exc.status_code if isinstance(exc, GenXError) else None,
                    "session_id": session_id,
                    "message_id": message_id,
                    "remote_job_id": remote_job_id,
                    "model": selected.model_id,
                },
            )
            raise

        self._audit_session_phase(
            "genx.remote_job_terminal",
            reconciled,
            reconciled.requested_metadata,
            status=reconciled.status,
            source="POLL",
        )
        history: dict[str, Any] = {}
        assistant_text = ""
        try:
            history = self.client.session_messages(session_id)
            assistant_text = extract_session_assistant_text(
                history,
                job_id=remote_job_id,
                message_id=message_id,
            )
        except GenXError as exc:
            AuditEvent.objects.create(
                severity="WARNING",
                event_type="genx.session_history_unavailable",
                actor="genx-gateway",
                metadata={
                    "call_id": str(call.id),
                    "session_id": session_id,
                    "remote_job_id": remote_job_id,
                    "error_code": exc.__class__.__name__,
                },
            )
        if not assistant_text:
            assistant_text = decode_text_result_url(reconciled.result_url)
        if assistant_text:
            persisted = dict(reconciled.requested_metadata)
            persisted["assistant_text"] = assistant_text
            GenXCall.objects.filter(pk=reconciled.pk).update(requested_metadata=persisted)
            reconciled.requested_metadata = persisted
        try:
            self.client.close_session(session_id)
        except GenXError:
            AuditEvent.objects.create(
                severity="WARNING",
                event_type="genx.session_close_failed",
                actor="genx-gateway",
                metadata={"call_id": str(call.id), "session_id": session_id, "remote_job_id": remote_job_id},
            )
        return reconciled, {
            "submission": response,
            "remote_job": final,
            "session_history": history,
            "assistant_text": assistant_text,
            "result_url": reconciled.result_url,
        }

    @staticmethod
    def _session_id(call: GenXCall) -> str:
        value = str((call.requested_metadata or {}).get("session_id") or "")
        if value:
            return value
        if call.external_job_id.startswith("session:"):
            return call.external_job_id.split(":", 1)[1]
        return ""

    @staticmethod
    def _remote_job_id(call: GenXCall) -> str:
        value = str((call.requested_metadata or {}).get("remote_job_id") or "")
        if value:
            return value
        if call.external_job_id and not call.external_job_id.startswith("session:"):
            return call.external_job_id
        return ""

    @staticmethod
    def _audit_session_phase(event_type, call, metadata, *, status, source=""):
        AuditEvent.objects.create(
            event_type=event_type,
            actor="genx-gateway",
            metadata={
                "call_id": str(call.id),
                "session_id": str(metadata.get("session_id") or ""),
                "message_id": str(metadata.get("message_id") or ""),
                "remote_job_id": str(metadata.get("remote_job_id") or ""),
                "model": call.model,
                "status": status,
                "usage_source": source,
                "billing_truth": str(metadata.get("billing_truth") or ""),
            },
        )

    def reconcile_pending(self, limit: int = 100) -> dict[str, int]:
        reconciled = 0
        unresolved = 0
        candidates = GenXCall.objects.filter(
            Q(status__in=["SUBMITTED", "UNKNOWN_REMOTE_STATE"]) | Q(status="COMPLETED")
        ).order_by("created_at")
        calls = []
        for call in candidates:
            billing_truth = str((call.requested_metadata or {}).get("billing_truth") or "")
            if call.status != "COMPLETED" or billing_truth == "UNRESOLVED":
                calls.append(call)
            if len(calls) >= limit:
                break
        for call in calls:
            try:
                metadata = dict(call.requested_metadata or {})
                is_session = metadata.get("transport") == "session" or call.external_job_id.startswith("session:")
                session_id = self._session_id(call) if is_session else ""
                remote_job_id = self._remote_job_id(call)
                history: dict[str, Any] = {}
                if is_session and not remote_job_id:
                    if not session_id:
                        unresolved += 1
                        continue
                    history = self.client.session_messages(session_id)
                    discovered = session_assistant_job_ids(history)
                    if len(discovered) != 1:
                        self._open_identity_alert(call, discovered)
                        unresolved += 1
                        continue
                    remote_job_id = discovered[0]
                    metadata.update({"session_id": session_id, "remote_job_id": remote_job_id})
                    GenXCall.objects.filter(pk=call.pk).update(
                        external_job_id=remote_job_id,
                        requested_metadata=metadata,
                    )
                    call.external_job_id = remote_job_id
                    call.requested_metadata = metadata
                    self._audit_session_phase(
                        "genx.remote_job_identity_recovered", call, metadata, status=call.status, source="POLL"
                    )
                if not remote_job_id:
                    unresolved += 1
                    continue
                payload = self.client.job(remote_job_id)
                if str(payload.get("status") or "").lower() not in {"completed", "failed", "cancelled"}:
                    unresolved += 1
                    continue
                reconciled_call = self.reconcile_remote_job_payload(call.id, payload, source="POLL")
                if is_session and session_id:
                    if not history:
                        history = self.client.session_messages(session_id)
                    assistant_text = extract_session_assistant_text(history, job_id=remote_job_id)
                    if not assistant_text:
                        assistant_text = decode_text_result_url(reconciled_call.result_url)
                    if assistant_text:
                        updated_metadata = dict(reconciled_call.requested_metadata)
                        updated_metadata["assistant_text"] = assistant_text
                        GenXCall.objects.filter(pk=call.pk).update(requested_metadata=updated_metadata)
                reconciled += 1
            except (GenXError, TimeoutError):
                unresolved += 1
        return {"reconciled": reconciled, "unresolved": unresolved}

    @staticmethod
    def _open_identity_alert(call: GenXCall, discovered: list[str]):
        existing = Alert.objects.filter(
            alert_type="GENX_REMOTE_JOB_ID_AMBIGUOUS",
            status="OPEN",
            metadata__call_id=str(call.id),
        ).first()
        if not existing:
            Alert.objects.create(
                severity="WARNING",
                alert_type="GENX_REMOTE_JOB_ID_AMBIGUOUS",
                message="A historical GenX session has no single authoritative assistant job identity.",
                metadata={"call_id": str(call.id), "assistant_job_ids": discovered},
            )

    @transaction.atomic
    def reconcile_remote_job_payload(
        self,
        call_id,
        payload: dict[str, Any],
        source: str,
        elapsed_ms: int = 0,
    ) -> GenXCall:
        """Idempotently apply authoritative remote execution and billing evidence."""
        source = str(source).upper()
        if source not in {"POLL", "WEBHOOK", "OPERATOR_EVIDENCE"}:
            raise ValueError("unsupported GenX reconciliation source")
        call = GenXCall.objects.select_for_update().select_related("job").get(pk=call_id)
        was_terminal = call.status in {"COMPLETED", "FAILED", "CANCELLED"} and call.completed_at is not None
        status = str(payload.get("status") or "UNKNOWN").upper()
        if status not in {"COMPLETED", "FAILED", "CANCELLED"}:
            return call
        metadata = dict(call.requested_metadata or {})
        previous_billing_truth = str(metadata.get("billing_truth") or "")
        previous_credits = call.credits
        previous_cost = call.cost_equivalent
        usage_value = usage_credits(payload)
        if usage_value is not None and usage_value < 0:
            usage_value = None
        if usage_value is None and previous_billing_truth == "ACTUAL":
            billing_truth = "ACTUAL"
        elif usage_value is None:
            billing_truth = "UNRESOLVED"
        else:
            billing_truth = "ACTUAL"
            call.credits = usage_value
        metadata.update({
            "billing_truth": billing_truth,
            "billing_source": source,
            "remote_job_id": str(payload.get("job_id") or metadata.get("remote_job_id") or call.external_job_id),
        })
        accounting_currency = call.job.currency if call.job_id else str(metadata.get("accounting_currency") or "USD")
        metadata["accounting_currency"] = accounting_currency
        if usage_value is not None:
            resolved_cost = monetary_cost_for_credits(
                credits=usage_value,
                currency=accounting_currency,
                at=call.completed_at or timezone.now(),
            )
            if resolved_cost is not None:
                call.cost_equivalent = resolved_cost.amount
                metadata.update({
                    "cost_equivalent_truth": "ACTUAL",
                    "valuation_id": resolved_cost.valuation_id,
                    "valuation_version": resolved_cost.version,
                    "valuation_source": resolved_cost.source,
                    "valuation_effective_at": resolved_cost.effective_at,
                    "monetary_cost_per_credit": str(resolved_cost.monetary_cost_per_credit),
                })
            elif usage_value == ZERO:
                call.cost_equivalent = ZERO
                metadata["cost_equivalent_truth"] = "ACTUAL_ZERO_CREDITS"
            else:
                call.cost_equivalent = None
                metadata["cost_equivalent_truth"] = "UNRESOLVED_VALUATION"
        elif previous_billing_truth != "ACTUAL":
            call.cost_equivalent = None
            estimate = monetary_cost_for_credits(
                credits=call.estimated_credits,
                currency=accounting_currency,
                at=call.started_at or timezone.now(),
            )
            metadata["cost_equivalent_truth"] = "ESTIMATED_USAGE" if estimate else "UNRESOLVED"
            metadata["estimated_cost_equivalent"] = str(estimate.amount) if estimate else None
            if estimate:
                metadata.update({
                    "valuation_id": estimate.valuation_id,
                    "valuation_version": estimate.version,
                    "valuation_source": estimate.source,
                })
        call.status = status
        if usage_value is not None:
            call.usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {
                "credits": str(usage_value)
            }
        remote_result_url = result_url(payload)
        if remote_result_url:
            call.result_url = remote_result_url
        call.latency_ms = max(0, elapsed_ms)
        call.completed_at = call.completed_at or timezone.now()
        call.error_code = str(payload.get("error_code") or "")[:120]
        call.requested_metadata = metadata
        call.save(
            update_fields=[
                "status", "credits", "usage", "cost_equivalent", "requested_metadata", "result_url",
                "latency_ms", "completed_at", "error_code", "updated_at",
            ]
        )

        if not was_terminal:
            stat, _ = ModelStat.objects.select_for_update().get_or_create(model=call.model, task_class=call.task_class)
            stat.attempts += 1
            if status == "COMPLETED":
                stat.successful_executions += 1
            else:
                stat.failures += 1
                if call.error_code:
                    stat.provider_failures += 1
            if usage_value is not None:
                stat.credits += usage_value
            stat.total_latency_ms += call.latency_ms
            stat.save(update_fields=["attempts", "successful_executions", "failures", "provider_failures", "credits", "total_latency_ms", "updated_at"])
        elif usage_value is not None and previous_billing_truth != "ACTUAL":
            stat, _ = ModelStat.objects.select_for_update().get_or_create(model=call.model, task_class=call.task_class)
            stat.credits += usage_value - previous_credits
            stat.save(update_fields=["credits", "updated_at"])
        elif usage_value is not None and previous_billing_truth == "ACTUAL" and previous_credits != usage_value:
            stat, _ = ModelStat.objects.select_for_update().get_or_create(model=call.model, task_class=call.task_class)
            stat.credits += usage_value - previous_credits
            stat.save(update_fields=["credits", "updated_at"])

        if call.cost_equivalent is not None and call.cost_equivalent != previous_cost:
            stat, _ = ModelStat.objects.select_for_update().get_or_create(model=call.model, task_class=call.task_class)
            cost_delta = call.cost_equivalent - (previous_cost or ZERO)
            stat.cost_equivalent += cost_delta
            stat.profit -= cost_delta
            stat.net_profit -= cost_delta
            stat.save(update_fields=["cost_equivalent", "profit", "net_profit", "updated_at"])

        if status == "COMPLETED" and billing_truth == "UNRESOLVED":
            existing = Alert.objects.filter(
                alert_type="GENX_USAGE_MISSING", status="OPEN", metadata__call_id=str(call.id)
            ).first()
            if not existing:
                Alert.objects.create(
                    severity="WARNING",
                    alert_type="GENX_USAGE_MISSING",
                    message="Completed GenX call returned no authoritative credit usage; its estimate remains reserved.",
                    metadata={
                        "call_id": str(call.id),
                        "external_job_id": call.external_job_id,
                        "remote_job_id": metadata.get("remote_job_id"),
                    },
                )
            if previous_billing_truth != "UNRESOLVED":
                AuditEvent.objects.create(
                    severity="WARNING",
                    event_type="genx.billing_unresolved",
                    actor="genx-gateway",
                    metadata={
                        "call_id": str(call.id), "remote_job_id": metadata.get("remote_job_id"),
                        "model": call.model, "status": status, "usage_source": source,
                        "billing_truth": billing_truth,
                    },
                )
        if usage_value is not None:
            Alert.objects.filter(
                alert_type="GENX_USAGE_MISSING", status="OPEN", metadata__call_id=str(call.id)
            ).update(status="RESOLVED", resolved_at=timezone.now())
            if previous_billing_truth != "ACTUAL" or previous_credits != usage_value:
                AuditEvent.objects.create(
                    event_type="genx.billing_reconciled",
                    actor="genx-gateway",
                    metadata={
                        "call_id": str(call.id), "remote_job_id": metadata.get("remote_job_id"),
                        "model": call.model, "status": status, "usage_source": source,
                        "billing_truth": billing_truth, "credits": str(usage_value),
                    },
                )
        if usage_value is not None and usage_value > ZERO and call.cost_equivalent is None:
            Alert.objects.get_or_create(
                alert_type="GENX_CREDIT_VALUATION_MISSING",
                status="OPEN",
                metadata={"call_id": str(call.id), "currency": accounting_currency},
                defaults={
                    "severity": "CRITICAL",
                    "message": "Actual GenX credits are known but no authoritative monetary valuation is effective.",
                },
            )
        elif call.cost_equivalent is not None:
            Alert.objects.filter(
                alert_type="GENX_CREDIT_VALUATION_MISSING",
                status="OPEN",
                metadata__call_id=str(call.id),
            ).update(status="RESOLVED", resolved_at=timezone.now())
        if usage_value is not None and usage_value > call.max_allowed_credits:
            Alert.objects.get_or_create(
                alert_type="GENX_CALL_BUDGET_OVERRUN",
                status="OPEN",
                metadata={"call_id": str(call.id), "actual": str(usage_value), "limit": str(call.max_allowed_credits)},
                defaults={
                    "severity": "CRITICAL",
                    "message": "A completed GenX call exceeded its controller-side credit estimate ceiling.",
                },
            )
        if call.job_id:
            total = effective_reserved_credits(
                GenXCall.objects.filter(job_id=call.job_id).values_list(
                    "credits", "estimated_credits", "status", "requested_metadata"
                )
            )
            score = JobScore.objects.select_for_update().get(job_id=call.job_id)
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
                "session_id": str(metadata.get("session_id") or ""),
                "message_id": str(metadata.get("message_id") or ""),
                "remote_job_id": str(metadata.get("remote_job_id") or ""),
                "status": status,
                "credits": str(usage_value) if usage_value is not None else None,
                "model": call.model,
                "latency_ms": call.latency_ms,
                "usage_source": source,
                "billing_truth": billing_truth,
                "cost_equivalent": str(call.cost_equivalent) if call.cost_equivalent is not None else None,
                "cost_equivalent_truth": metadata.get("cost_equivalent_truth"),
                "valuation_version": metadata.get("valuation_version"),
            },
        )
        if call.job_id:
            from control.services.product_factory import refresh_product_cost_basis_for_job

            refresh_product_cost_basis_for_job(call.job_id)
        return call

    def reconcile(
        self,
        call_id,
        payload: dict[str, Any],
        elapsed_ms: int = 0,
        source: str = "POLL",
    ) -> GenXCall:
        """Compatibility boundary for generate/proxy callers and future authenticated sources."""
        return self.reconcile_remote_job_payload(call_id, payload, source=source, elapsed_ms=elapsed_ms)
