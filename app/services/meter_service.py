"""Idempotent metering, quota enforcement, and monthly rollup service."""

from datetime import datetime, timezone
import hashlib
import json
import uuid
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import UsageEvent
from app.repositories.billing_repository import BillingRepository, current_billing_period
from app.schemas import BillableRequest
from app.services.pricing_service import MICROCENTS_PER_CENT, PricingService


class BillingError(Exception):
    """Base domain error carrying HTTP status code, machine-readable code, and headers."""

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        details: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.details = details or {}
        self.headers = headers or {}


class MeterService:
    """Handles idempotent usage recording, boundary quota enforcement, and usage rollups."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = BillingRepository(db)

    @staticmethod
    def _compute_request_hash(payload: BillableRequest) -> str:
        canonical = json.dumps(
            {
                "tenant_id": payload.tenant_id,
                "usage_type": payload.usage_type,
                "quantity": payload.quantity,
                "input_tokens": payload.input_tokens,
                "cached_input_tokens": payload.cached_input_tokens,
                "output_tokens": payload.output_tokens,
                "reasoning_tokens": payload.reasoning_tokens,
                "require_pro": payload.require_pro,
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def record_billable_action(
        self, payload: BillableRequest, idempotency_key: str
    ) -> dict:
        """Record a usage event idempotently after verifying plan status and monthly quotas.

        Order of operations:
        1. Validate tenant exists.
        2. Check idempotency key (`(tenant_id, idempotency_key)`):
           - If found with matching request payload hash -> return stored response (`idempotent_replay=True`).
           - If found with different request payload hash -> raise 409 Conflict.
        3. Check payment / subscription status (`402 Payment Required` if unpaid/past_due/canceled,
           if `require_pro=True` on Free plan, or if tenant's hard spend budget is exceeded).
        4. Check plan quota boundary (`current_used + requested_quantity <= plan_limit`):
           - At `999 / 1,000` + `1` = `1,000 / 1,000` -> ALLOWED (`200 OK`).
           - At `1,000 / 1,000` + `1` = `1,001 > 1,000` -> REJECTED (`429 Too Many Requests` + `Retry-After`).
        5. Calculate integer cost in micro-cents and persist `UsageEvent` inside transaction.
        """
        tenant = self.repo.get_tenant(payload.tenant_id)
        if tenant is None:
            raise BillingError(
                status_code=404,
                error_code="tenant_not_found",
                message=f"Tenant '{payload.tenant_id}' does not exist.",
            )

        plan = self.repo.get_plan(tenant.plan_id)
        if plan is None:
            raise BillingError(
                status_code=500,
                error_code="plan_not_configured",
                message=f"Plan '{tenant.plan_id}' is missing from database.",
            )

        req_hash = self._compute_request_hash(payload)

        # Step 1: Idempotency check BEFORE quota enforcement or mutation
        existing_event = self.repo.get_usage_event_by_idempotency_key(
            tenant_id=tenant.id, idempotency_key=idempotency_key
        )
        if existing_event is not None:
            if existing_event.request_hash != req_hash:
                raise BillingError(
                    status_code=409,
                    error_code="idempotency_key_conflict",
                    message=(
                        f"Idempotency-Key '{idempotency_key}' was already used with different "
                        f"request parameters for tenant '{tenant.id}'."
                    ),
                )
            snapshot = json.loads(existing_event.response_snapshot)
            snapshot["idempotent_replay"] = True
            return snapshot

        # Step 2: 402 Payment Required checks (subscription state, Pro-gated feature, or budget guard)
        if tenant.subscription_status in ("past_due", "unpaid", "canceled"):
            raise BillingError(
                status_code=402,
                error_code="payment_required_subscription_inactive",
                message=(
                    f"Payment required: tenant '{tenant.id}' subscription status is "
                    f"'{tenant.subscription_status}'. Please complete payment or renew subscription."
                ),
                details={
                    "tenant_id": tenant.id,
                    "plan_id": tenant.plan_id,
                    "subscription_status": tenant.subscription_status,
                },
            )

        if payload.require_pro and tenant.plan_id == "free":
            raise BillingError(
                status_code=402,
                error_code="payment_required_upgrade_plan",
                message=(
                    f"Payment required: tenant '{tenant.id}' is on the 'free' plan. "
                    f"Upgrade to the 'pro' plan via POST /billing/checkout to access this action."
                ),
                details={
                    "tenant_id": tenant.id,
                    "current_plan": tenant.plan_id,
                    "required_plan": "pro",
                },
            )

        period = current_billing_period()
        quantity = int(payload.quantity or 1)

        # Calculate event cost in integer micro-cents
        if payload.usage_type == "api_call":
            event_cost_mc = PricingService.calculate_api_call_cost_microcents(quantity)
            plan_limit = int(plan.api_calls_limit)
        else:
            breakdown = PricingService.calculate_ai_tokens_cost_breakdown(
                input_tokens=payload.input_tokens,
                cached_input_tokens=payload.cached_input_tokens,
                output_tokens=payload.output_tokens,
                reasoning_tokens=payload.reasoning_tokens,
            )
            event_cost_mc = breakdown["total_token_cost_microcents"]
            plan_limit = int(plan.ai_tokens_limit)

        # Budget guard check (Shared Requirement #7: Cost tracked, attributed, with a budget guard)
        if tenant.budget_limit_microcents is not None:
            current_cost_mc = self.repo.get_monthly_used_cost_microcents(tenant.id, period)
            if current_cost_mc + event_cost_mc > int(tenant.budget_limit_microcents):
                raise BillingError(
                    status_code=402,
                    error_code="payment_required_budget_exceeded",
                    message=(
                        f"Payment required: action would exceed monthly spend budget guard for tenant '{tenant.id}' "
                        f"(current: {current_cost_mc} microcents, requested: {event_cost_mc} microcents, "
                        f"budget_limit: {tenant.budget_limit_microcents} microcents)."
                    ),
                    details={
                        "tenant_id": tenant.id,
                        "current_cost_microcents": current_cost_mc,
                        "requested_cost_microcents": event_cost_mc,
                        "budget_limit_microcents": int(tenant.budget_limit_microcents),
                    },
                )

        # Step 3: Quota Boundary Enforcement (429 Too Many Requests when used + requested > limit)
        current_used = self.repo.get_monthly_used_quantity(
            tenant_id=tenant.id, usage_type=payload.usage_type, billing_period=period
        )
        projected_used = current_used + quantity
        if projected_used > plan_limit:
            upgrade_hint = (
                " Upgrade from 'free' to 'pro' via POST /billing/checkout for higher limits."
                if tenant.plan_id == "free"
                else " Wait until next billing cycle or contact support."
            )
            raise BillingError(
                status_code=429,
                error_code="quota_exceeded",
                message=(
                    f"Monthly '{payload.usage_type}' quota exceeded for tenant '{tenant.id}' on plan "
                    f"'{tenant.plan_id}': current usage is {current_used}/{plan_limit}, requested "
                    f"{quantity} (projected {projected_used} > limit {plan_limit}).{upgrade_hint}"
                ),
                details={
                    "tenant_id": tenant.id,
                    "plan_id": tenant.plan_id,
                    "usage_type": payload.usage_type,
                    "current_used": current_used,
                    "requested": quantity,
                    "limit": plan_limit,
                    "remaining": max(0, plan_limit - current_used),
                },
                headers={"Retry-After": "3600"},
            )

        # Step 4: Record event atomically
        event_id = f"evt_{uuid.uuid4().hex[:16]}"
        now_iso = datetime.now(timezone.utc).isoformat()
        remaining_after = plan_limit - projected_used

        response_data = {
            "event_id": event_id,
            "tenant_id": tenant.id,
            "plan_id": tenant.plan_id,
            "usage_type": payload.usage_type,
            "quantity_recorded": quantity,
            "idempotency_key": idempotency_key,
            "idempotent_replay": False,
            "billing_period": period,
            "quota": {
                "usage_type": payload.usage_type,
                "used": projected_used,
                "limit": plan_limit,
                "remaining": remaining_after,
                "at_exact_boundary": projected_used == plan_limit,
            },
            "event_cost": PricingService.microcents_to_cost_summary(event_cost_mc),
            "simulated_output": (
                f"Simulated AI response for tenant '{tenant.id}' ({quantity} tokens metered)."
                if payload.usage_type == "ai_tokens"
                else f"Simulated billable API call completed for tenant '{tenant.id}'."
            ),
            "created_at": now_iso,
        }

        usage_event = UsageEvent(
            id=event_id,
            tenant_id=tenant.id,
            idempotency_key=idempotency_key,
            request_hash=req_hash,
            usage_type=payload.usage_type,
            quantity=quantity,
            input_tokens=payload.input_tokens,
            cached_input_tokens=payload.cached_input_tokens,
            output_tokens=payload.output_tokens,
            reasoning_tokens=payload.reasoning_tokens,
            cost_microcents=event_cost_mc,
            billing_period=period,
            response_snapshot=json.dumps(response_data),
        )

        try:
            self.repo.create_usage_event(usage_event)
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            # Concurrent race with same (tenant_id, idempotency_key)
            existing = self.repo.get_usage_event_by_idempotency_key(tenant.id, idempotency_key)
            if existing is not None and existing.request_hash == req_hash:
                snapshot = json.loads(existing.response_snapshot)
                snapshot["idempotent_replay"] = True
                return snapshot
            raise

        return response_data

    def get_tenant_usage_rollup(self, tenant_id: str, billing_period: str | None = None) -> dict:
        """Compute monthly usage and exact integer cost rollup for a tenant (`GET /usage`)."""
        tenant = self.repo.get_tenant(tenant_id)
        if tenant is None:
            raise BillingError(
                status_code=404,
                error_code="tenant_not_found",
                message=f"Tenant '{tenant_id}' does not exist.",
            )

        plan = self.repo.get_plan(tenant.plan_id)
        if plan is None:
            raise BillingError(
                status_code=500,
                error_code="plan_not_configured",
                message=f"Plan '{tenant.plan_id}' is missing from database.",
            )

        period = billing_period or current_billing_period()

        api_calls_used = self.repo.get_monthly_used_quantity(tenant.id, "api_call", period)
        api_calls_limit = int(plan.api_calls_limit)
        api_calls_remaining = max(0, api_calls_limit - api_calls_used)
        api_calls_pct = (api_calls_used * 100 // api_calls_limit) if api_calls_limit > 0 else 0

        ai_tokens_used = self.repo.get_monthly_used_quantity(tenant.id, "ai_tokens", period)
        ai_tokens_limit = int(plan.ai_tokens_limit)
        ai_tokens_remaining = max(0, ai_tokens_limit - ai_tokens_used)
        ai_tokens_pct = (ai_tokens_used * 100 // ai_tokens_limit) if ai_tokens_limit > 0 else 0

        token_counts = self.repo.get_monthly_token_breakdown(tenant.id, period)
        token_cost_breakdown = PricingService.calculate_ai_tokens_cost_breakdown(
            input_tokens=token_counts["input_tokens"],
            cached_input_tokens=token_counts["cached_input_tokens"],
            output_tokens=token_counts["output_tokens"],
            reasoning_tokens=token_counts["reasoning_tokens"],
        )

        api_calls_cost_mc = PricingService.calculate_api_call_cost_microcents(api_calls_used)
        ai_tokens_cost_mc = token_cost_breakdown["total_token_cost_microcents"]
        usage_cost_mc = api_calls_cost_mc + ai_tokens_cost_mc
        base_plan_cost_mc = int(plan.base_price_cents) * MICROCENTS_PER_CENT
        total_monthly_cost_mc = base_plan_cost_mc + usage_cost_mc

        alerts = self.repo.list_alerts_for_tenant(tenant.id, period)

        return {
            "tenant_id": tenant.id,
            "tenant_name": tenant.name,
            "plan_id": tenant.plan_id,
            "subscription_status": tenant.subscription_status,
            "billing_period": period,
            # Top-level rollup convenience fields alongside detailed breakdown
            "used": {
                "api_calls": api_calls_used,
                "ai_tokens": ai_tokens_used,
            },
            "limit": {
                "api_calls": api_calls_limit,
                "ai_tokens": ai_tokens_limit,
            },
            "api_calls": {
                "used": api_calls_used,
                "limit": api_calls_limit,
                "remaining": api_calls_remaining,
                "utilization_pct": api_calls_pct,
            },
            "ai_tokens": {
                "used": ai_tokens_used,
                "limit": ai_tokens_limit,
                "remaining": ai_tokens_remaining,
                "utilization_pct": ai_tokens_pct,
            },
            "token_breakdown": token_cost_breakdown,
            "cost": {
                "currency": "USD",
                "api_calls_cost": PricingService.microcents_to_cost_summary(api_calls_cost_mc),
                "ai_tokens_cost": PricingService.microcents_to_cost_summary(ai_tokens_cost_mc),
                "usage_total": PricingService.microcents_to_cost_summary(usage_cost_mc),
                "plan_base_fee": PricingService.microcents_to_cost_summary(base_plan_cost_mc),
                "monthly_total": PricingService.microcents_to_cost_summary(total_monthly_cost_mc),
            },
            "pricing_rules_pinned": PricingService.get_pinned_pricing_config(),
            "alerts_triggered": [
                {
                    "usage_type": a.usage_type,
                    "threshold_pct": a.threshold_pct,
                    "current_used": a.current_used,
                    "plan_limit": a.plan_limit,
                    "message": a.message,
                }
                for a in alerts
            ],
        }
