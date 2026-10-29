"""FastAPI HTTP routes separating HTTP transport from business and data layers."""

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from app.db.database import get_db
from app.repositories.billing_repository import BillingRepository
from app.schemas import BillableRequest, CheckoutRequest, TenantCreateRequest
from app.services.job_service import BackgroundJobService
from app.services.meter_service import BillingError, MeterService
from app.services.pricing_service import PricingService
from app.services.stripe_service import StripeService

router = APIRouter()


def _run_usage_alert_check_in_background(tenant_id: str, db_bind) -> None:
    """Off-request-path background task checking 80% and 100% quota alert thresholds."""
    BgSession = sessionmaker(bind=db_bind, autoflush=False, autocommit=False, expire_on_commit=False)
    with BgSession() as bg_db:
        job_service = BackgroundJobService(bg_db)
        job_service.evaluate_tenant_usage_alerts(tenant_id)


@router.get("/health")
def health_check() -> dict:
    """Liveness and configuration probe."""
    return {
        "status": "ok",
        "service": "usage-metering-billing-engine",
        "pricing": PricingService.get_pinned_pricing_config(),
    }


@router.get("/plans")
def list_plans(db: Session = Depends(get_db)) -> dict:
    """Return configured subscription plans and monthly quotas."""
    repo = BillingRepository(db)
    free = repo.get_plan("free")
    pro = repo.get_plan("pro")
    plans = [p for p in (free, pro) if p is not None]
    return {
        "plans": [
            {
                "id": p.id,
                "name": p.name,
                "api_calls_limit": p.api_calls_limit,
                "ai_tokens_limit": p.ai_tokens_limit,
                "base_price_cents": p.base_price_cents,
            }
            for p in plans
        ]
    }


@router.get("/tenants")
def list_tenants(db: Session = Depends(get_db)) -> dict:
    """List isolated tenants."""
    repo = BillingRepository(db)
    tenants = repo.list_tenants()
    return {
        "tenants": [
            {
                "id": t.id,
                "name": t.name,
                "email": t.email,
                "plan_id": t.plan_id,
                "subscription_status": t.subscription_status,
                "stripe_customer_id": t.stripe_customer_id,
                "budget_limit_microcents": t.budget_limit_microcents,
            }
            for t in tenants
        ]
    }


@router.post("/tenants", status_code=201)
def create_or_update_tenant(
    payload: TenantCreateRequest, db: Session = Depends(get_db)
) -> dict:
    """Create or update a tenant for testing."""
    repo = BillingRepository(db)
    if repo.get_plan(payload.plan_id) is None:
        raise BillingError(
            status_code=400,
            error_code="invalid_plan",
            message=f"Plan '{payload.plan_id}' does not exist.",
        )
    tenant = repo.upsert_tenant(
        tenant_id=payload.id,
        name=payload.name,
        email=payload.email,
        plan_id=payload.plan_id,
        subscription_status=payload.subscription_status,
        budget_limit_microcents=payload.budget_limit_microcents,
    )
    db.commit()
    return {
        "id": tenant.id,
        "name": tenant.name,
        "email": tenant.email,
        "plan_id": tenant.plan_id,
        "subscription_status": tenant.subscription_status,
        "budget_limit_microcents": tenant.budget_limit_microcents,
    }


@router.post("/generate")
@router.post("/meter")
def billable_generate_endpoint(
    payload: BillableRequest,
    background_tasks: BackgroundTasks,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
) -> dict:
    """Dummy billable endpoint (`POST /generate` and `POST /meter`).

    - Requires an idempotency key (`Idempotency-Key` header or `idempotency_key` body field).
    - Enforces subscription status (`402`) and monthly quota limits (`429`) before recording usage.
    - Deduplicates retries so the exact same request + key creates only one usage event.
    - Enqueues an asynchronous background job to evaluate 80% / 100% quota alerts.
    """
    effective_key = (idempotency_key_header or payload.idempotency_key or "").strip()
    if not effective_key:
        raise BillingError(
            status_code=400,
            error_code="missing_idempotency_key",
            message="Missing idempotency key. Provide 'Idempotency-Key' header or 'idempotency_key' in body.",
        )

    meter_service = MeterService(db)
    result = meter_service.record_billable_action(payload=payload, idempotency_key=effective_key)

    if not result.get("idempotent_replay"):
        background_tasks.add_task(_run_usage_alert_check_in_background, payload.tenant_id, db.get_bind())

    return result


@router.get("/usage")
def get_usage_rollup(
    tenant_id: str | None = Query(default=None, description="Tenant ID to query usage for"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    billing_period: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    db: Session = Depends(get_db),
) -> dict:
    """Roll up monthly usage events, plan limits, and exact integer costs (`GET /usage`)."""
    resolved_tenant = (tenant_id or x_tenant_id or "tenant_free").strip()
    meter_service = MeterService(db)
    return meter_service.get_tenant_usage_rollup(
        tenant_id=resolved_tenant, billing_period=billing_period
    )


@router.get("/usage/{tenant_id}")
def get_usage_rollup_by_path(
    tenant_id: str,
    billing_period: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    db: Session = Depends(get_db),
) -> dict:
    """Path-parameter convenience route for `GET /usage/{tenant_id}`."""
    meter_service = MeterService(db)
    return meter_service.get_tenant_usage_rollup(
        tenant_id=tenant_id.strip(), billing_period=billing_period
    )


@router.post("/billing/checkout")
@router.post("/checkout")
def create_stripe_checkout(
    payload: CheckoutRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Create a Stripe test-mode Checkout Session to upgrade a tenant to Pro."""
    stripe_service = StripeService(db)
    return stripe_service.create_checkout_session(payload)


@router.post("/webhooks/stripe")
async def stripe_webhook_endpoint(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    db: Session = Depends(get_db),
) -> dict:
    """Receive and verify Stripe webhooks using the raw request body.

    - Verifies `Stripe-Signature` (`400 Bad Request` on forged or malformed signature).
    - Deduplicates replayed events by `event.id` (`ignored_duplicate`).
    - Updates tenant plan and subscription status on `checkout.session.completed`,
      `customer.subscription.updated`, and `customer.subscription.deleted`.
    """
    raw_body = await request.body()
    stripe_service = StripeService(db)
    return stripe_service.process_webhook(raw_body=raw_body, sig_header=stripe_signature)


class ReconcileJobRequest(BaseModel):
    stripe_snapshot: list[dict] | None = Field(default=None)
    simulate_transient_failures: int = Field(default=0, ge=0, le=10)


@router.post("/jobs/reconcile")
def trigger_reconciliation_job(
    payload: ReconcileJobRequest | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """Run the background Stripe reconciliation & usage alert job with retry and failure-alert handling."""
    job_service = BackgroundJobService(db)
    req = payload or ReconcileJobRequest()
    return job_service.run_reconciliation_job(
        stripe_snapshot=req.stripe_snapshot,
        simulate_transient_failures=req.simulate_transient_failures,
    )


@router.get("/jobs")
def list_background_jobs(db: Session = Depends(get_db)) -> dict:
    """Inspect recent background job executions, retry counts, and failure alerts."""
    repo = BillingRepository(db)
    jobs = repo.list_background_jobs()
    return {
        "jobs": [
            {
                "id": j.id,
                "job_type": j.job_type,
                "tenant_id": j.tenant_id,
                "status": j.status,
                "attempts": j.attempts,
                "max_retries": j.max_retries,
                "details": j.details,
                "failure_alert": j.failure_alert,
                "created_at": j.created_at,
            }
            for j in jobs
        ]
    }
