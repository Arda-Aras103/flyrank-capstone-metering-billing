"""Database seed logic for demo plans and tenants."""

from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.billing_repository import BillingRepository


def seed_demo_data(db: Session) -> dict:
    """Idempotently seed the Free and Pro plans along with demo tenants."""
    repo = BillingRepository(db)

    free_plan = repo.upsert_plan(
        plan_id="free",
        name="Free Plan",
        api_calls_limit=settings.free_plan_api_calls_limit,
        ai_tokens_limit=settings.free_plan_ai_tokens_limit,
        base_price_cents=settings.free_plan_base_price_cents,
        stripe_price_id=None,
    )

    pro_plan = repo.upsert_plan(
        plan_id="pro",
        name="Pro Plan",
        api_calls_limit=settings.pro_plan_api_calls_limit,
        ai_tokens_limit=settings.pro_plan_ai_tokens_limit,
        base_price_cents=settings.pro_plan_base_price_cents,
        stripe_price_id=settings.stripe_pro_price_id,
    )

    tenant_free = repo.upsert_tenant(
        tenant_id="tenant_free",
        name="Acme Startup (Free Tier)",
        email="billing@acme-free.example.com",
        plan_id="free",
        subscription_status="active",
        stripe_customer_id="cus_test_tenant_free",
    )

    tenant_pro = repo.upsert_tenant(
        tenant_id="tenant_pro",
        name="Globex Enterprise (Pro Tier)",
        email="finance@globex-pro.example.com",
        plan_id="pro",
        subscription_status="active",
        stripe_customer_id="cus_test_tenant_pro",
    )

    tenant_unpaid = repo.upsert_tenant(
        tenant_id="tenant_unpaid",
        name="Initech Past-Due Org",
        email="accounts@initech-unpaid.example.com",
        plan_id="pro",
        subscription_status="past_due",
        stripe_customer_id="cus_test_tenant_unpaid",
    )

    db.commit()
    return {
        "plans_seeded": [free_plan.id, pro_plan.id],
        "tenants_seeded": [tenant_free.id, tenant_pro.id, tenant_unpaid.id],
    }
