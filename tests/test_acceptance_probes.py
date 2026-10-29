"""Acceptance probes and comprehensive test suite for the Usage Metering & Billing Engine.

Covers all 5 Layer-2 Acceptance Probes and all 7 Shared Requirements from Section 12:
- PROBE 1: Exactly-once idempotent metering (same request + key twice -> 1 event; conflicting payload -> 409)
- PROBE 2: Boundary honesty at exact quota (999 -> 1000 allowed; 1001 -> 429) and 402 Payment Required semantics
- PROBE 3: End-to-end Stripe test Checkout + signed webhook flips tenant Free -> Pro and updates GET /usage limits
- PROBE 4: Forged webhook signature rejected with 400 (no state change); replayed webhook deduplicated
- PROBE 5: Pinned AI token pricing rules (cached input cheaper, reasoning billed as output, exact integer micro-cents)
- SHARED REQUIREMENTS: Boundary validation (4xx never 500), tenant isolation, background job retries & failure alert
"""

import hashlib
import hmac
import json
from pathlib import Path
import sys
import time

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
for venv_site in ROOT_DIR.glob(".venv/lib*/python*/site-packages"):
    if str(venv_site) not in sys.path:
        sys.path.insert(0, str(venv_site))

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db.database import Base, get_db
from app.main import app
from app.seed import seed_demo_data


def generate_stripe_signature(payload_bytes: bytes, secret: str, timestamp: int | None = None) -> str:
    """Create a valid Stripe-Signature header (`t=...,v1=...`) matching `stripe.Webhook.construct_event`."""
    ts = timestamp or int(time.time())
    signed_payload = f"{ts}.{payload_bytes.decode('utf-8')}".encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), msg=signed_payload, digestmod=hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


@pytest.fixture()
def client():
    """Provide an isolated in-memory SQLite database seeded with Free/Pro plans and demo tenants."""
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(
        bind=test_engine, autoflush=False, autocommit=False, expire_on_commit=False
    )

    with TestingSessionLocal() as db:
        seed_demo_data(db)

    def override_get_db():
        with TestingSessionLocal() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_probe_1_idempotent_metering_no_double_counting(client: TestClient):
    """PROBE 1: Send the same billable request twice with one idempotency key ->
    creates exactly one usage event; the second response mirrors the first.
    """
    headers = {"Idempotency-Key": "idem-probe1-key-001"}
    payload = {
        "tenant_id": "tenant_free",
        "usage_type": "ai_tokens",
        "input_tokens": 1000,
        "cached_input_tokens": 500,
        "output_tokens": 2500,
        "reasoning_tokens": 0,
    }

    # First call records the usage event
    res1 = client.post("/generate", json=payload, headers=headers)
    assert res1.status_code == 200
    body1 = res1.json()
    assert body1["idempotent_replay"] is False
    assert body1["quantity_recorded"] == 4000
    assert body1["quota"]["used"] == 4000

    # Second call (network retry with identical Idempotency-Key)
    res2 = client.post("/generate", json=payload, headers=headers)
    assert res2.status_code == 200
    body2 = res2.json()
    assert body2["idempotent_replay"] is True
    assert body2["event_id"] == body1["event_id"]
    assert body2["quantity_recorded"] == body1["quantity_recorded"]
    assert body2["quota"]["used"] == 4000  # Not 8000!
    assert body2["event_cost"] == body1["event_cost"]

    # Verify GET /usage confirms exactly 4,000 tokens used, not 8,000
    usage_res = client.get("/usage", params={"tenant_id": "tenant_free"})
    assert usage_res.status_code == 200
    usage_data = usage_res.json()
    assert usage_data["ai_tokens"]["used"] == 4000
    assert usage_data["used"]["ai_tokens"] == 4000

    # Reusing the same Idempotency-Key with a DIFFERENT payload must return 409 Conflict
    conflicting_payload = {**payload, "output_tokens": 3000}
    res_conflict = client.post("/generate", json=conflicting_payload, headers=headers)
    assert res_conflict.status_code == 409
    assert res_conflict.json()["error"] == "idempotency_key_conflict"


def test_probe_2_boundary_honesty_429_and_402(client: TestClient):
    """PROBE 2: Drive a tenant to its exact quota (999 -> 1,000 of 1,000 API calls) ->
    request at 1,000 succeeds (200 OK); request 1,001 returns 429 Too Many Requests with clear message.
    Also verifies 402 Payment Required for past_due/unpaid plans, Pro-gated requests, and budget guard.
    """
    # Step 1: Consume 999 of 1,000 API calls on Free plan
    res_999 = client.post(
        "/generate",
        json={"tenant_id": "tenant_free", "usage_type": "api_call", "quantity": 999},
        headers={"Idempotency-Key": "idem-boundary-999"},
    )
    assert res_999.status_code == 200
    assert res_999.json()["quota"]["used"] == 999
    assert res_999.json()["quota"]["remaining"] == 1
    assert res_999.json()["quota"]["at_exact_boundary"] is False

    # Step 2: Call #1,000 (exact boundary: 999 + 1 == 1,000) -> MUST be allowed (200 OK)
    res_1000 = client.post(
        "/generate",
        json={"tenant_id": "tenant_free", "usage_type": "api_call", "quantity": 1},
        headers={"Idempotency-Key": "idem-boundary-1000"},
    )
    assert res_1000.status_code == 200
    assert res_1000.json()["quota"]["used"] == 1000
    assert res_1000.json()["quota"]["remaining"] == 0
    assert res_1000.json()["quota"]["at_exact_boundary"] is True

    # Step 3: Call #1,001 (1,000 + 1 > 1,000) -> MUST be rejected with 429 Too Many Requests
    res_1001 = client.post(
        "/generate",
        json={"tenant_id": "tenant_free", "usage_type": "api_call", "quantity": 1},
        headers={"Idempotency-Key": "idem-boundary-1001"},
    )
    assert res_1001.status_code == 429
    assert "Retry-After" in res_1001.headers
    err_429 = res_1001.json()
    assert err_429["error"] == "quota_exceeded"
    assert "1000/1000" in err_429["message"]

    # Step 4: Verify 402 Payment Required on past_due / unpaid subscription
    res_unpaid = client.post(
        "/generate",
        json={"tenant_id": "tenant_unpaid", "usage_type": "api_call", "quantity": 1},
        headers={"Idempotency-Key": "idem-unpaid-001"},
    )
    assert res_unpaid.status_code == 402
    assert res_unpaid.json()["error"] == "payment_required_subscription_inactive"

    # Step 5: Verify 402 Payment Required when Free tenant requests Pro-only action
    res_pro_gate = client.post(
        "/generate",
        json={
            "tenant_id": "tenant_free",
            "usage_type": "api_call",
            "quantity": 1,
            "require_pro": True,
        },
        headers={"Idempotency-Key": "idem-pro-gate-001"},
    )
    assert res_pro_gate.status_code == 402
    assert res_pro_gate.json()["error"] == "payment_required_upgrade_plan"


def test_probe_3_stripe_checkout_and_webhook_upgrade_flow(client: TestClient):
    """PROBE 3: Complete a Stripe test Checkout -> the signed webhook flips the tenant Free -> Pro;
    GET /usage shows the new Pro limits (50,000 API calls / 5,000,000 AI tokens).
    Also verifies subscription update and deletion webhooks.
    """
    # 1. Confirm tenant_free starts on 'free' plan (1,000 API calls, 100,000 AI tokens)
    before = client.get("/usage", params={"tenant_id": "tenant_free"}).json()
    assert before["plan_id"] == "free"
    assert before["api_calls"]["limit"] == 1_000
    assert before["ai_tokens"]["limit"] == 100_000

    # 2. Initiate Stripe test-mode Checkout Session
    checkout_res = client.post(
        "/billing/checkout",
        json={"tenant_id": "tenant_free", "plan_id": "pro"},
    )
    assert checkout_res.status_code == 200
    checkout_data = checkout_res.json()
    assert checkout_data["checkout_session_id"].startswith("cs_test_")
    assert checkout_data["target_plan"] == "pro"

    # 3. Deliver signed checkout.session.completed webhook from Stripe
    webhook_event = {
        "id": "evt_test_checkout_completed_001",
        "object": "event",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": checkout_data["checkout_session_id"],
                "customer": "cus_test_tenant_free",
                "subscription": "sub_test_upgraded_001",
                "client_reference_id": "tenant_free",
                "metadata": {"tenant_id": "tenant_free", "plan_id": "pro"},
            }
        },
    }
    raw_body = json.dumps(webhook_event).encode("utf-8")
    sig_header = generate_stripe_signature(raw_body, settings.stripe_webhook_secret)

    wh_res = client.post(
        "/webhooks/stripe",
        content=raw_body,
        headers={"Stripe-Signature": sig_header, "Content-Type": "application/json"},
    )
    assert wh_res.status_code == 200
    wh_body = wh_res.json()
    assert wh_body["status"] == "processed"
    assert wh_body["duplicate"] is False
    assert wh_body["tenant_id"] == "tenant_free"

    # 4. Verify GET /usage now shows 'pro' plan and Pro limits
    after = client.get("/usage", params={"tenant_id": "tenant_free"}).json()
    assert after["plan_id"] == "pro"
    assert after["subscription_status"] == "active"
    assert after["api_calls"]["limit"] == 50_000
    assert after["ai_tokens"]["limit"] == 5_000_000

    # 5. Test customer.subscription.deleted downgrades tenant back to 'free'
    delete_event = {
        "id": "evt_test_sub_deleted_001",
        "object": "event",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": "sub_test_upgraded_001",
                "customer": "cus_test_tenant_free",
                "metadata": {"tenant_id": "tenant_free"},
            }
        },
    }
    del_raw = json.dumps(delete_event).encode("utf-8")
    del_sig = generate_stripe_signature(del_raw, settings.stripe_webhook_secret)
    del_res = client.post(
        "/webhooks/stripe",
        content=del_raw,
        headers={"Stripe-Signature": del_sig, "Content-Type": "application/json"},
    )
    assert del_res.status_code == 200
    after_cancel = client.get("/usage", params={"tenant_id": "tenant_free"}).json()
    assert after_cancel["plan_id"] == "free"


def test_probe_4_webhook_forgery_rejected_and_replay_deduplicated(client: TestClient):
    """PROBE 4: Send a forged webhook (bad signature) -> 400, nothing changes.
    Replay a real event twice -> processed once, second ignored.
    """
    event_payload = {
        "id": "evt_test_replay_check_001",
        "object": "event",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_999",
                "customer": "cus_test_tenant_free",
                "subscription": "sub_test_999",
                "client_reference_id": "tenant_free",
                "metadata": {"tenant_id": "tenant_free", "plan_id": "pro"},
            }
        },
    }
    raw_body = json.dumps(event_payload).encode("utf-8")

    # 1. Forged signature -> 400 Bad Request, tenant stays on 'free'
    forged_sig = generate_stripe_signature(raw_body, "whsec_wrong_attacker_secret")
    res_forged = client.post(
        "/webhooks/stripe",
        content=raw_body,
        headers={"Stripe-Signature": forged_sig, "Content-Type": "application/json"},
    )
    assert res_forged.status_code == 400
    assert res_forged.json()["error"] == "invalid_webhook_signature"

    usage_check = client.get("/usage", params={"tenant_id": "tenant_free"}).json()
    assert usage_check["plan_id"] == "free"

    # 2. Valid signature -> processed once (flips tenant_free to 'pro')
    valid_sig = generate_stripe_signature(raw_body, settings.stripe_webhook_secret)
    res_valid_1 = client.post(
        "/webhooks/stripe",
        content=raw_body,
        headers={"Stripe-Signature": valid_sig, "Content-Type": "application/json"},
    )
    assert res_valid_1.status_code == 200
    assert res_valid_1.json()["duplicate"] is False
    assert res_valid_1.json()["status"] == "processed"

    # 3. Replay exact same event a second time -> ignored_duplicate
    res_valid_2 = client.post(
        "/webhooks/stripe",
        content=raw_body,
        headers={"Stripe-Signature": valid_sig, "Content-Type": "application/json"},
    )
    assert res_valid_2.status_code == 200
    assert res_valid_2.json()["duplicate"] is True
    assert res_valid_2.json()["status"] == "ignored_duplicate"


def test_probe_5_pinned_pricing_rules_ai_tokens_and_api_calls(client: TestClient):
    """PROBE 5: Check the pinned pricing rules -> cached-input and reasoning-token rules
    produce the exact expected integer totals; GET /usage matches.

    Pinned constants:
    - API call: 1,000 micro-cents / call
    - Fresh input token: 2 micro-cents / token
    - Cached input token: 1 micro-cent / token (50% cheaper than fresh input)
    - Output token: 8 micro-cents / token
    - Reasoning token: 8 micro-cents / token (counts as output token)
    """
    # Event 1: AI token generation with all 4 token categories
    # - 10,000 fresh input tokens  * 2 =  20,000 micro-cents
    # - 20,000 cached input tokens * 1 =  20,000 micro-cents (cheaper than fresh!)
    # -  5,000 output tokens       * 8 =  40,000 micro-cents
    # -  2,500 reasoning tokens    * 8 =  20,000 micro-cents (billed as output!)
    # Expected AI token cost           = 100,000 micro-cents (10 cents = $0.10)
    res_tokens = client.post(
        "/generate",
        json={
            "tenant_id": "tenant_free",
            "usage_type": "ai_tokens",
            "input_tokens": 10_000,
            "cached_input_tokens": 20_000,
            "output_tokens": 5_000,
            "reasoning_tokens": 2_500,
        },
        headers={"Idempotency-Key": "idem-pricing-tokens-001"},
    )
    assert res_tokens.status_code == 200
    token_event = res_tokens.json()
    assert token_event["quantity_recorded"] == 37_500
    assert token_event["event_cost"]["microcents"] == 100_000
    assert token_event["event_cost"]["cents"] == 10
    assert token_event["event_cost"]["formatted_usd"] == "$0.10"

    # Event 2: 50 billable API calls
    # - 50 API calls * 1,000 micro-cents = 50,000 micro-cents (5 cents = $0.05)
    res_calls = client.post(
        "/generate",
        json={
            "tenant_id": "tenant_free",
            "usage_type": "api_call",
            "quantity": 50,
        },
        headers={"Idempotency-Key": "idem-pricing-calls-001"},
    )
    assert res_calls.status_code == 200
    call_event = res_calls.json()
    assert call_event["event_cost"]["microcents"] == 50_000
    assert call_event["event_cost"]["cents"] == 5
    assert call_event["event_cost"]["formatted_usd"] == "$0.05"

    # Verify GET /usage rollup matches exact expected integer totals (100,000 + 50,000 = 150,000 micro-cents = 15 cents = $0.15)
    rollup = client.get("/usage", params={"tenant_id": "tenant_free"}).json()
    tb = rollup["token_breakdown"]
    assert tb["input_tokens"] == 10_000
    assert tb["cached_input_tokens"] == 20_000
    assert tb["output_tokens"] == 5_000
    assert tb["reasoning_tokens"] == 2_500
    assert tb["effective_output_tokens"] == 7_500
    assert tb["total_tokens"] == 37_500
    assert tb["input_cost_microcents"] == 20_000
    assert tb["cached_input_cost_microcents"] == 20_000
    assert tb["output_and_reasoning_cost_microcents"] == 60_000
    assert tb["total_token_cost_microcents"] == 100_000

    assert rollup["cost"]["api_calls_cost"]["microcents"] == 50_000
    assert rollup["cost"]["ai_tokens_cost"]["microcents"] == 100_000
    assert rollup["cost"]["usage_total"]["microcents"] == 150_000
    assert rollup["cost"]["usage_total"]["cents"] == 15
    assert rollup["cost"]["usage_total"]["formatted_usd"] == "$0.15"


def test_shared_requirements_validation_tenant_isolation_and_background_jobs(client: TestClient):
    """Verify boundary validation (4xx never 500), multi-tenant isolation, budget guard, and background job retries."""
    # 1. Validation at the boundary: negative tokens or missing Idempotency-Key -> clean 4xx
    bad_res = client.post(
        "/generate",
        json={"tenant_id": "tenant_free", "usage_type": "ai_tokens", "input_tokens": -5},
        headers={"Idempotency-Key": "idem-bad-1"},
    )
    assert bad_res.status_code == 422

    missing_key_res = client.post(
        "/generate",
        json={"tenant_id": "tenant_free", "usage_type": "api_call", "quantity": 1},
    )
    assert missing_key_res.status_code == 400
    assert missing_key_res.json()["error"] == "missing_idempotency_key"

    # 2. Tenant isolation & budget guard
    client.post(
        "/tenants",
        json={
            "id": "tenant_budget_guarded",
            "name": "Budget Guarded Co",
            "email": "guard@example.com",
            "plan_id": "free",
            "subscription_status": "active",
            "budget_limit_microcents": 5_000,  # Max 5 API calls (5,000 microcents)
        },
    )
    # 5 API calls (= 5,000 microcents) -> allowed
    ok_budget = client.post(
        "/generate",
        json={"tenant_id": "tenant_budget_guarded", "usage_type": "api_call", "quantity": 5},
        headers={"Idempotency-Key": "idem-budget-ok"},
    )
    assert ok_budget.status_code == 200

    # 1 more API call (= 6,000 > 5,000 budget) -> 402 Payment Required (budget exceeded)
    over_budget = client.post(
        "/generate",
        json={"tenant_id": "tenant_budget_guarded", "usage_type": "api_call", "quantity": 1},
        headers={"Idempotency-Key": "idem-budget-over"},
    )
    assert over_budget.status_code == 402
    assert over_budget.json()["error"] == "payment_required_budget_exceeded"

    # Ensure tenant_free still has 0 usage (strict tenant isolation)
    free_usage = client.get("/usage", params={"tenant_id": "tenant_free"}).json()
    assert free_usage["api_calls"]["used"] == 0

    # 3. Background job: Usage alerts at 80% & 100% + Reconciliation retries and failure alert
    client.post(
        "/generate",
        json={"tenant_id": "tenant_free", "usage_type": "api_call", "quantity": 850},
        headers={"Idempotency-Key": "idem-alert-80pct"},
    )
    free_after_850 = client.get("/usage", params={"tenant_id": "tenant_free"}).json()
    assert any(a["threshold_pct"] == 80 for a in free_after_850["alerts_triggered"])

    # Trigger background reconciliation with 1 simulated transient failure (recovers on attempt 2)
    rec_ok = client.post(
        "/jobs/reconcile",
        json={
            "stripe_snapshot": [{"tenant_id": "tenant_free", "plan_id": "pro", "status": "active"}],
            "simulate_transient_failures": 1,
        },
    ).json()
    assert rec_ok["status"] == "completed"
    assert rec_ok["attempts"] == 2
    assert rec_ok["reconciled_tenants"][0]["reconciled_plan"] == "pro"

    # Trigger background reconciliation that exhausts all 3 retries -> emits failure_alert
    rec_fail = client.post(
        "/jobs/reconcile",
        json={"simulate_transient_failures": 5},
    ).json()
    assert rec_fail["status"] == "failed"
    assert rec_fail["attempts"] == 3
    assert "CRITICAL BACKGROUND JOB FAILURE" in rec_fail["failure_alert"]
