"""Repository encapsulating all SQL queries and persistence operations."""

from datetime import datetime, timezone
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    BackgroundJob,
    Plan,
    Subscription,
    Tenant,
    UsageAlert,
    UsageEvent,
    WebhookEvent,
)


def current_billing_period() -> str:
    """Return current UTC billing period formatted as YYYY-MM."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


class BillingRepository:
    """Data-layer operations isolated per tenant."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # --- Plans ---
    def get_plan(self, plan_id: str) -> Plan | None:
        return self.db.get(Plan, plan_id)

    def upsert_plan(
        self,
        plan_id: str,
        name: str,
        api_calls_limit: int,
        ai_tokens_limit: int,
        base_price_cents: int,
        stripe_price_id: str | None = None,
    ) -> Plan:
        plan = self.db.get(Plan, plan_id)
        if plan is None:
            plan = Plan(
                id=plan_id,
                name=name,
                api_calls_limit=api_calls_limit,
                ai_tokens_limit=ai_tokens_limit,
                base_price_cents=base_price_cents,
                stripe_price_id=stripe_price_id,
            )
            self.db.add(plan)
        else:
            plan.name = name
            plan.api_calls_limit = api_calls_limit
            plan.ai_tokens_limit = ai_tokens_limit
            plan.base_price_cents = base_price_cents
            plan.stripe_price_id = stripe_price_id
        self.db.flush()
        return plan

    # --- Tenants ---
    def get_tenant(self, tenant_id: str) -> Tenant | None:
        return self.db.get(Tenant, tenant_id)

    def get_tenant_by_stripe_customer(self, stripe_customer_id: str) -> Tenant | None:
        stmt = select(Tenant).where(Tenant.stripe_customer_id == stripe_customer_id)
        return self.db.scalar(stmt)

    def get_tenant_by_stripe_subscription(self, stripe_subscription_id: str) -> Tenant | None:
        stmt = select(Tenant).where(Tenant.stripe_subscription_id == stripe_subscription_id)
        return self.db.scalar(stmt)

    def list_tenants(self) -> list[Tenant]:
        stmt = select(Tenant).order_by(Tenant.id)
        return list(self.db.scalars(stmt).all())

    def upsert_tenant(
        self,
        tenant_id: str,
        name: str,
        email: str,
        plan_id: str = "free",
        subscription_status: str = "active",
        stripe_customer_id: str | None = None,
        budget_limit_microcents: int | None = None,
    ) -> Tenant:
        tenant = self.db.get(Tenant, tenant_id)
        if tenant is None:
            tenant = Tenant(
                id=tenant_id,
                name=name,
                email=email,
                plan_id=plan_id,
                subscription_status=subscription_status,
                stripe_customer_id=stripe_customer_id,
                budget_limit_microcents=budget_limit_microcents,
            )
            self.db.add(tenant)
        else:
            tenant.name = name
            tenant.email = email
            tenant.plan_id = plan_id
            tenant.subscription_status = subscription_status
            if stripe_customer_id is not None:
                tenant.stripe_customer_id = stripe_customer_id
            tenant.budget_limit_microcents = budget_limit_microcents
        self.db.flush()
        return tenant

    # --- Usage Events & Rollups ---
    def get_usage_event_by_idempotency_key(
        self, tenant_id: str, idempotency_key: str
    ) -> UsageEvent | None:
        stmt = select(UsageEvent).where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.idempotency_key == idempotency_key,
        )
        return self.db.scalar(stmt)

    def get_monthly_used_quantity(
        self, tenant_id: str, usage_type: str, billing_period: str
    ) -> int:
        stmt = select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.usage_type == usage_type,
            UsageEvent.billing_period == billing_period,
        )
        return int(self.db.scalar(stmt) or 0)

    def get_monthly_used_cost_microcents(
        self, tenant_id: str, billing_period: str
    ) -> int:
        stmt = select(func.coalesce(func.sum(UsageEvent.cost_microcents), 0)).where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.billing_period == billing_period,
        )
        return int(self.db.scalar(stmt) or 0)

    def get_monthly_token_breakdown(
        self, tenant_id: str, billing_period: str
    ) -> dict[str, int]:
        stmt = select(
            func.coalesce(func.sum(UsageEvent.input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.cached_input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.output_tokens), 0),
            func.coalesce(func.sum(UsageEvent.reasoning_tokens), 0),
        ).where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.usage_type == "ai_tokens",
            UsageEvent.billing_period == billing_period,
        )
        row = self.db.execute(stmt).one()
        return {
            "input_tokens": int(row[0] or 0),
            "cached_input_tokens": int(row[1] or 0),
            "output_tokens": int(row[2] or 0),
            "reasoning_tokens": int(row[3] or 0),
        }

    def create_usage_event(self, event: UsageEvent) -> UsageEvent:
        self.db.add(event)
        self.db.flush()
        return event

    # --- Webhooks & Subscriptions ---
    def get_webhook_event(self, stripe_event_id: str) -> WebhookEvent | None:
        return self.db.get(WebhookEvent, stripe_event_id)

    def record_webhook_event(
        self,
        stripe_event_id: str,
        event_type: str,
        payload_hash: str,
        tenant_id: str | None = None,
        status: str = "processed",
    ) -> WebhookEvent:
        record = WebhookEvent(
            stripe_event_id=stripe_event_id,
            event_type=event_type,
            tenant_id=tenant_id,
            status=status,
            payload_hash=payload_hash,
        )
        self.db.add(record)
        self.db.flush()
        return record

    def upsert_subscription(
        self,
        sub_id: str,
        tenant_id: str,
        stripe_subscription_id: str,
        stripe_customer_id: str,
        plan_id: str,
        status: str,
        current_period_start: datetime | None = None,
        current_period_end: datetime | None = None,
    ) -> Subscription:
        stmt = select(Subscription).where(
            Subscription.stripe_subscription_id == stripe_subscription_id
        )
        sub = self.db.scalar(stmt)
        if sub is None:
            sub = Subscription(
                id=sub_id,
                tenant_id=tenant_id,
                stripe_subscription_id=stripe_subscription_id,
                stripe_customer_id=stripe_customer_id,
                plan_id=plan_id,
                status=status,
                current_period_start=current_period_start,
                current_period_end=current_period_end,
            )
            self.db.add(sub)
        else:
            sub.tenant_id = tenant_id
            sub.stripe_customer_id = stripe_customer_id
            sub.plan_id = plan_id
            sub.status = status
            sub.current_period_start = current_period_start
            sub.current_period_end = current_period_end
        self.db.flush()
        return sub

    def list_subscriptions_for_tenant(self, tenant_id: str) -> list[Subscription]:
        stmt = select(Subscription).where(Subscription.tenant_id == tenant_id)
        return list(self.db.scalars(stmt).all())

    # --- Usage Alerts & Background Jobs ---
    def list_alerts_for_tenant(self, tenant_id: str, billing_period: str) -> list[UsageAlert]:
        stmt = (
            select(UsageAlert)
            .where(
                UsageAlert.tenant_id == tenant_id,
                UsageAlert.billing_period == billing_period,
            )
            .order_by(UsageAlert.threshold_pct.asc())
        )
        return list(self.db.scalars(stmt).all())

    def create_alert_if_not_exists(
        self,
        alert_id: str,
        tenant_id: str,
        billing_period: str,
        usage_type: str,
        threshold_pct: int,
        current_used: int,
        plan_limit: int,
        message: str,
    ) -> UsageAlert | None:
        stmt = select(UsageAlert).where(
            UsageAlert.tenant_id == tenant_id,
            UsageAlert.billing_period == billing_period,
            UsageAlert.usage_type == usage_type,
            UsageAlert.threshold_pct == threshold_pct,
        )
        existing = self.db.scalar(stmt)
        if existing is not None:
            return None
        alert = UsageAlert(
            id=alert_id,
            tenant_id=tenant_id,
            billing_period=billing_period,
            usage_type=usage_type,
            threshold_pct=threshold_pct,
            current_used=current_used,
            plan_limit=plan_limit,
            message=message,
        )
        self.db.add(alert)
        self.db.flush()
        return alert

    def record_background_job(self, job: BackgroundJob) -> BackgroundJob:
        self.db.add(job)
        self.db.flush()
        return job

    def list_background_jobs(self, limit: int = 20) -> list[BackgroundJob]:
        stmt = select(BackgroundJob).order_by(BackgroundJob.created_at.desc()).limit(limit)
        return list(self.db.scalars(stmt).all())
