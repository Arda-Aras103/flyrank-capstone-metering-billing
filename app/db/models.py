"""SQLAlchemy ORM models for tenants, plans, subscriptions, usage_events, webhooks, and jobs."""

from datetime import datetime, timezone
from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    """Subscription tier with monthly quotas and base price in integer cents."""

    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    api_calls_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    ai_tokens_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    base_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stripe_price_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Tenant(Base):
    """Customer organization isolated by tenant_id."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(32), ForeignKey("plans.id"), nullable=False, index=True)
    subscription_status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    budget_limit_microcents: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Subscription(Base):
    """Local mirror of a tenant's Stripe subscription state."""

    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False, index=True)
    stripe_subscription_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    stripe_customer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(32), ForeignKey("plans.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class UsageEvent(Base):
    """Idempotent ledger entry of a single billable action."""

    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_tenant_idempotency"),
        Index("idx_usage_events_tenant_period_type", "tenant_id", "billing_period", "usage_type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    usage_type: Mapped[str] = mapped_column(String(32), nullable=False)  # "api_call" | "ai_tokens"
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_microcents: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), nullable=False, default=0
    )
    billing_period: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    response_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class WebhookEvent(Base):
    """Deduplication log of verified Stripe webhook events."""

    __tablename__ = "webhook_events"

    stripe_event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="processed")
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UsageAlert(Base):
    """Threshold alert record emitted when a tenant crosses 80% or 100% of monthly quota."""

    __tablename__ = "usage_alerts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "billing_period",
            "usage_type",
            "threshold_pct",
            name="uq_tenant_period_type_threshold",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False, index=True)
    billing_period: Mapped[str] = mapped_column(String(7), nullable=False)
    usage_type: Mapped[str] = mapped_column(String(32), nullable=False)
    threshold_pct: Mapped[int] = mapped_column(Integer, nullable=False)
    current_used: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class BackgroundJob(Base):
    """Execution record for off-request background jobs (reconciliation, usage alerts) with retry tracking."""

    __tablename__ = "background_jobs"
    __table_args__ = (Index("idx_background_jobs_type_status", "job_type", "status"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # completed | failed | retrying
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_alert: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
