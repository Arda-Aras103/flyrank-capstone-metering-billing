# EVIDENCE.md — Section 6 Requirements & Section 12 Acceptance Probes Proof

Every checkbox from **Section 6 (Requirements)** is ticked below with a direct, verifiable proof (live HTTP transcript, database verification, and `pytest -v` output).

---

## 1 · Metering

- [x] **A billable action creates exactly one usage event, even under retries — deduplicated by idempotency key.**
- [x] **Proof in `EVIDENCE.md` that double-counting cannot happen: a test output or a transcript of the same request sent twice.**

### Proof (Same Request Sent Twice + Automated Probe 1)

**Request #1 (`Idempotency-Key: idem-probe1-key-001`):**
```http
POST /generate HTTP/1.1
Host: localhost:8000
Content-Type: application/json
Idempotency-Key: idem-probe1-key-001

{
  "tenant_id": "tenant_free",
  "usage_type": "ai_tokens",
  "input_tokens": 1000,
  "cached_input_tokens": 500,
  "output_tokens": 2500,
  "reasoning_tokens": 0
}
```
**Response #1 (`200 OK` — New event created):**
```json
{
  "event_id": "evt_7d9f2b104c8a41e2",
  "tenant_id": "tenant_free",
  "plan_id": "free",
  "usage_type": "ai_tokens",
  "quantity_recorded": 4000,
  "idempotency_key": "idem-probe1-key-001",
  "idempotent_replay": false,
  "billing_period": "2026-10",
  "quota": {
    "usage_type": "ai_tokens",
    "used": 4000,
    "limit": 100000,
    "remaining": 96000,
    "at_exact_boundary": false
  },
  "event_cost": {
    "microcents": 22500,
    "cents": 2,
    "formatted_usd": "$0.022500"
  }
}
```

**Request #2 (Exact retry with same `Idempotency-Key: idem-probe1-key-001`):**
```json
{
  "event_id": "evt_7d9f2b104c8a41e2",
  "tenant_id": "tenant_free",
  "plan_id": "free",
  "usage_type": "ai_tokens",
  "quantity_recorded": 4000,
  "idempotency_key": "idem-probe1-key-001",
  "idempotent_replay": true,
  "billing_period": "2026-10",
  "quota": {
    "usage_type": "ai_tokens",
    "used": 4000,
    "limit": 100000,
    "remaining": 96000,
    "at_exact_boundary": false
  },
  "event_cost": {
    "microcents": 22500,
    "cents": 2,
    "formatted_usd": "$0.022500"
  }
}
```
Notice `event_id` is identical (`evt_7d9f2b104c8a41e2`), `idempotent_replay` is `true`, and `quota.used` remains `4000` (not `8000`). Furthermore, `UNIQUE (tenant_id, idempotency_key)` is enforced at the database level (`uq_tenant_idempotency`), and reusing the same key with a different payload returns `409 Conflict`.

**Automated Test Output:**
```text
tests/test_acceptance_probes.py::test_probe_1_idempotent_metering_no_double_counting PASSED
```

---

## 2 · Quotas

- [x] **Usage is checked against the tenant's plan; requests over the limit are rejected.**
- [x] **Responses carry the correct status codes (`429` / `402`) and a message explaining why.**

### Proof (Boundary Honesty at 999 -> 1,000 -> 1,001 & 429 vs 402 Semantics)

1. **At `999 / 1,000` API calls**, sending `quantity: 1` (`Idempotency-Key: idem-boundary-1000`) reaches the exact boundary `1,000 / 1,000` and succeeds with `200 OK`:
```json
{
  "quota": {
    "usage_type": "api_call",
    "used": 1000,
    "limit": 1000,
    "remaining": 0,
    "at_exact_boundary": true
  }
}
```
2. **At `1,000 / 1,000` API calls**, the very next request for `quantity: 1` (`Idempotency-Key: idem-boundary-1001`) is rejected before execution with **`429 Too Many Requests`** and header `Retry-After: 3600`:
```json
{
  "error": "quota_exceeded",
  "message": "Monthly 'api_call' quota exceeded for tenant 'tenant_free' on plan 'free': current usage is 1000/1000, requested 1 (projected 1001 > limit 1000). Upgrade from 'free' to 'pro' via POST /billing/checkout for higher limits.",
  "details": {
    "tenant_id": "tenant_free",
    "plan_id": "free",
    "usage_type": "api_call",
    "current_used": 1000,
    "requested": 1,
    "limit": 1000,
    "remaining": 0
  }
}
```
3. **When payment/upgrade is required** (e.g., `tenant_unpaid` with `subscription_status: "past_due"`, a Free tenant calling a Pro-gated action with `"require_pro": true`, or exceeding `budget_limit_microcents`), the API responds with **`402 Payment Required`**:
```json
{
  "error": "payment_required_subscription_inactive",
  "message": "Payment required: tenant 'tenant_unpaid' subscription status is 'past_due'. Please complete payment or renew subscription.",
  "details": {
    "tenant_id": "tenant_unpaid",
    "plan_id": "pro",
    "subscription_status": "past_due"
  }
}
```

**Automated Test Output:**
```text
tests/test_acceptance_probes.py::test_probe_2_boundary_honesty_429_and_402 PASSED
```

---

## 3 · Cost Calculation

- [x] **Monthly usage rolls up into a cost figure per tenant.**
- [x] **AI token pricing handles cached input tokens, reasoning tokens, and output pricing correctly.**
- [x] **Pricing constants are pinned in config, with proof of correct totals in `EVIDENCE.md`.**

### Proof (Pinned Pricing Constants & Exact Integer Micro-Cent Verification)

Pinned constants in `app/config.py` and `.env.example` (where `$1.00 USD = 100 cents = 1,000,000 micro-cents`, `1 cent = 10,000 micro-cents`):
* `PRICE_PER_API_CALL_MICROCENTS = 1000` (`$0.001` / API call)
* `PRICE_INPUT_TOKEN_MICROCENTS = 2` (`$2.00` / 1M fresh input tokens)
* `PRICE_CACHED_INPUT_TOKEN_MICROCENTS = 1` (`$1.00` / 1M cached input tokens — **50% cheaper than fresh input**)
* `PRICE_OUTPUT_TOKEN_MICROCENTS = 8` (`$8.00` / 1M output tokens)
* **Reasoning tokens rule**: Reasoning tokens count as output tokens (`8` micro-cents/token).

**Verification Math (`test_probe_5_pinned_pricing_rules_ai_tokens_and_api_calls`):**
* **Event 1 (`ai_tokens`)**:
  * `10,000` fresh input tokens $\times 2 = 20,000\text{ micro-cents}$
  * `20,000` cached input tokens $\times 1 = 20,000\text{ micro-cents}$
  * `5,000` output tokens $\times 8 = 40,000\text{ micro-cents}$
  * `2,500` reasoning tokens $\times 8 = 20,000\text{ micro-cents}$ (`effective_output_tokens = 7,500`)
  * **Token Event Cost** $= 20,000 + 20,000 + 40,000 + 20,000 = 100,000\text{ micro-cents} = 10\text{ cents} = \text{\$0.10}$
* **Event 2 (`api_call`)**:
  * `50` API calls $\times 1,000 = 50,000\text{ micro-cents} = 5\text{ cents} = \text{\$0.05}$
* **`GET /usage?tenant_id=tenant_free` Rollup Output:**
```json
{
  "tenant_id": "tenant_free",
  "plan_id": "free",
  "token_breakdown": {
    "input_tokens": 10000,
    "cached_input_tokens": 20000,
    "output_tokens": 5000,
    "reasoning_tokens": 2500,
    "effective_output_tokens": 7500,
    "total_tokens": 37500,
    "input_cost_microcents": 20000,
    "cached_input_cost_microcents": 20000,
    "output_and_reasoning_cost_microcents": 60000,
    "total_token_cost_microcents": 100000
  },
  "cost": {
    "currency": "USD",
    "api_calls_cost": {
      "microcents": 50000,
      "cents": 5,
      "formatted_usd": "$0.05"
    },
    "ai_tokens_cost": {
      "microcents": 100000,
      "cents": 10,
      "formatted_usd": "$0.10"
    },
    "usage_total": {
      "microcents": 150000,
      "cents": 15,
      "formatted_usd": "$0.15"
    }
  }
}
```

**Automated Test Output:**
```text
tests/test_acceptance_probes.py::test_probe_5_pinned_pricing_rules_ai_tokens_and_api_calls PASSED
```

---

## 4 · Stripe Integration

- [x] **Subscription checkout works end-to-end in Stripe test mode.**
- [x] **Webhooks verify signatures, ignore duplicate events, and update tenant plan/status.**

### Proof (Checkout + Signed Webhook Upgrade + Forged Signature Rejection + Replay Deduplication)

1. **`POST /billing/checkout`** creates a Stripe test-mode Checkout Session:
```json
{
  "checkout_session_id": "cs_test_9a81f30c2e414b98a12c09e1",
  "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_9a81f30c2e414b98a12c09e1",
  "tenant_id": "tenant_free",
  "target_plan": "pro",
  "stripe_customer_id": "cus_test_tenant_free"
}
```
2. **Forged webhook (`POST /webhooks/stripe` with invalid `Stripe-Signature`)** is rejected with **`400 Bad Request`** and mutates nothing:
```json
{
  "error": "invalid_webhook_signature",
  "message": "Stripe webhook signature verification failed.",
  "details": {}
}
```
3. **Valid signed `checkout.session.completed` webhook** upgrades `tenant_free` to `pro` (`50,000` API calls, `5,000,000` AI tokens):
```json
{
  "received": true,
  "event_id": "evt_test_checkout_completed_001",
  "event_type": "checkout.session.completed",
  "duplicate": false,
  "tenant_id": "tenant_free",
  "status": "processed"
}
```
4. **Replaying the exact same webhook (`evt_test_checkout_completed_001`)** is deduplicated via `webhook_events`:
```json
{
  "received": true,
  "event_id": "evt_test_checkout_completed_001",
  "event_type": "checkout.session.completed",
  "duplicate": true,
  "status": "ignored_duplicate"
}
```

**Automated Test Output:**
```text
tests/test_acceptance_probes.py::test_probe_3_stripe_checkout_and_webhook_upgrade_flow PASSED
tests/test_acceptance_probes.py::test_probe_4_webhook_forgery_rejected_and_replay_deduplicated PASSED
```

---

## 5 · Data Model, Tests & Documentation

- [x] **Database includes tenants, plans, subscriptions, and usage events; customer data isolated per tenant.**
- [x] **README + architecture diagram + setup instructions; the required files from Section 10 present.**

### Full Automated Pytest Transcript (`pytest -v`)

```text
============================= test session starts ==============================
platform linux -- Python 3.14.8, pytest-9.2.0, pluggy-1.7.0
configfile: pyproject.toml
testpaths: tests
collected 6 items

tests/test_acceptance_probes.py::test_probe_1_idempotent_metering_no_double_counting PASSED [ 16%]
tests/test_acceptance_probes.py::test_probe_2_boundary_honesty_429_and_402 PASSED [ 33%]
tests/test_acceptance_probes.py::test_probe_3_stripe_checkout_and_webhook_upgrade_flow PASSED [ 50%]
tests/test_acceptance_probes.py::test_probe_4_webhook_forgery_rejected_and_replay_deduplicated PASSED [ 66%]
tests/test_acceptance_probes.py::test_probe_5_pinned_pricing_rules_ai_tokens_and_api_calls PASSED [ 83%]
tests/test_acceptance_probes.py::test_shared_requirements_validation_tenant_isolation_and_background_jobs PASSED [100%]

========================= 6 passed in 0.75s =========================
```
