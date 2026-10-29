# Usage Metering & Billing Engine

**FlyRank Backend Track Capstone** — A multi-tenant SaaS usage metering, quota enforcement, AI-token cost calculator, and Stripe test-mode subscription billing engine built with **Python + FastAPI + SQLAlchemy (SQLite / PostgreSQL)**.

---

## 1. What the System Does

Every SaaS product must answer three questions accurately under retries, concurrency, and webhook replays:
1. **How much has this tenant used?** — Every billable action (`POST /generate` or `POST /meter`) records an idempotent usage event (`api_call` or `ai_tokens`) deduplicated by `(tenant_id, idempotency_key)`. Retrying the same request with the same `Idempotency-Key` returns the original result (`idempotent_replay: true`) and never double-counts or double-charges.
2. **Have they reached their plan limits?** — Quotas are checked **before** allowing a billable action:
   - At `999 / 1,000` calls, requesting `1` call brings usage to `1,000 / 1,000` and succeeds (`200 OK`, `at_exact_boundary: true`).
   - At `1,000 / 1,000` calls, the next request is rejected with **`429 Too Many Requests`** (`Retry-After: 3600`) and a clear explanation.
   - Inactive/unpaid subscriptions (`past_due`, `unpaid`, `canceled`), Free-tier calls to Pro-gated actions (`require_pro: true`), or requests exceeding a tenant's monthly spend cap (`budget_limit_microcents`) return **`402 Payment Required`**.
3. **What does it cost?** — All money is stored and computed as **integers** (`microcents` and `cents`, where `$1.00 USD = 100 cents = 1,000,000 microcents`), never IEEE-754 floats. AI token pricing strictly separates fresh input tokens, cheaper cached input tokens, output tokens, and reasoning tokens (which count as output tokens).
4. **Stripe Test-Mode Subscription Sync & Background Reconciliation** — Tenants upgrade from `free` to `pro` via `POST /billing/checkout`. `POST /webhooks/stripe` verifies the cryptographic `Stripe-Signature` against the raw request body (`400 Bad Request` on forgery), deduplicates replayed events via `webhook_events`, and updates the tenant's plan/status. Asynchronous background jobs evaluate 80%/100% usage alerts and reconcile database state with retries and failure alerts (`POST /jobs/reconcile`).

---

## 2. Architecture Diagram

```text
+------------------+       POST /generate (Idempotency-Key)       +-----------------------------------+
|                  | -------------------------------------------> | HTTP Layer (app/api/routes.py)    |
|  SaaS Client /   |       GET  /usage?tenant_id=...              | - Pydantic boundary validation    |
|  Evaluator Probe |       POST /billing/checkout                 | - Raw body capture for webhooks   |
+------------------+                                              +-----------------+-----------------+
                                                                                    |
          +-------------------------------------------------------------------------+----------------------------------+
          |                                                                         |                                  |
          v                                                                         v                                  v
+-----------------------------------------+                     +-----------------------------------+      +-----------------------+
| MeterService & PricingService           |                     | StripeService                     |      | BackgroundJobService  |
| (app/services/meter_service.py,         |                     | (app/services/stripe_service.py)  |      | (app/services/        |
|  app/services/pricing_service.py)       |                     | 1. Verify Stripe-Signature        |      |  job_service.py)      |
| 1. Check (tenant_id, idempotency_key)   |                     |    (forged -> 400 Bad Request)    |      | - Off-request 80%/100%|
|    - duplicate? -> return snapshot      |                     | 2. Deduplicate stripe_event_id    |      |   quota alert worker  |
|    - hash mismatch? -> 409 Conflict     |                     |    (replay -> ignored_duplicate)  |      | - Nightly Stripe DB   |
| 2. Check status/budget -> 402 if unpaid |                     | 3. Sync checkout.session.completed|      |   reconciliation with |
| 3. Check quota -> 429 if > limit        |                     |    subscription.updated / deleted |      |   retries & alerts    |
| 4. Compute integer micro-cent cost      |                     +-----------------+-----------------+      +-----------+-----------+
+--------------------+--------------------+                                       |                                    |
                     |                                                            |                                    |
                     +------------------------------------------------------------+------------------------------------+
                                                                                  |
                                                                                  v
                                                        +---------------------------------------------------+
                                                        | Data Layer: BillingRepository (SQLAlchemy 2.0)    |
                                                        | (app/repositories/billing_repository.py)          |
                                                        +-------------------------+-------------------------+
                                                                                  |
                                                                                  v
                                                        +---------------------------------------------------+
                                                        | Database (SQLite default / PostgreSQL via Docker) |
                                                        | Tables: plans, tenants, subscriptions,            |
                                                        | usage_events [UNIQUE(tenant_id, idempotency_key)],|
                                                        | webhook_events, usage_alerts, background_jobs     |
                                                        +---------------------------------------------------+
```

---

## 3. Plan Quotas & Pinned Pricing Constants

### Monthly Plan Quotas
| Plan ID | Monthly API Calls Quota | Monthly AI Tokens Quota | Base Subscription Fee |
| :--- | ---: | ---: | ---: |
| `free` | `1,000` / month | `100,000` / month | `$0.00` (`0` cents) |
| `pro` | `50,000` / month | `5,000,000` / month | `$49.00` (`4,900` cents) |

### Pinned Usage Pricing Constants (`app/config.py` & `.env.example`)
All usage costs are calculated in integer **micro-cents** (`1 USD = 100 cents = 1,000,000 micro-cents`; `1 cent = 10,000 micro-cents`):
* **API Calls (`api_call`)**: `1,000` micro-cents per call (`$0.001` / call = `$1.00` per 1,000 calls)
* **Fresh Input Tokens (`input_tokens`)**: `2` micro-cents per token (`$2.00` per 1M tokens)
* **Cached Input Tokens (`cached_input_tokens`)**: `1` micro-cent per token (`$1.00` per 1M tokens — **50% cheaper than fresh input**)
* **Output Tokens (`output_tokens`)**: `8` micro-cents per token (`$8.00` per 1M tokens)
* **Reasoning Tokens (`reasoning_tokens`)**: `8` micro-cents per token (**counts as output tokens** and is billed at the output token rate)

$$\text{Total Token Cost (micro-cents)} = (\text{input\_tokens} \times 2) + (\text{cached\_input\_tokens} \times 1) + ((\text{output\_tokens} + \text{reasoning\_tokens}) \times 8)$$

---

## 4. Exact Setup, Seed, Run, and Test Instructions

### Prerequisites
* **Python 3.11+** (Zero external services required for default SQLite mode; Docker optional if using PostgreSQL).

### Step 1: Clone & Install Dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Step 2: Seed Demo Plans & Tenants
Seeds the `free` and `pro` plans along with isolated demo tenants (`tenant_free`, `tenant_pro`, `tenant_unpaid`):
```bash
python3 scripts/seed.py
```

### Step 3: Run the Server (One Command)
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
*(Note: The FastAPI startup lifespan also runs migrations and seeds baseline plans/tenants idempotently on boot, so `uvicorn app.main:app --host 0.0.0.0 --port 8000` works on a completely clean machine in one command.)*

### Step 4: Run the Automated Acceptance Probe Suite
```bash
pytest -v
```

### Optional: Run with PostgreSQL via Docker Compose
```bash
docker compose up -d
export DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/metering_billing"
python3 scripts/seed.py
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Optional: Local Webhook Replay with Stripe CLI
```bash
stripe listen --forward-to localhost:8000/webhooks/stripe
stripe trigger checkout.session.completed
```

---

## 5. Quick Manual Probe Commands (`curl`)

```bash
# 1. Record an AI token usage event (send twice to verify idempotency)
curl -s -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-key-001" \
  -d '{
    "tenant_id": "tenant_free",
    "usage_type": "ai_tokens",
    "input_tokens": 10000,
    "cached_input_tokens": 20000,
    "output_tokens": 5000,
    "reasoning_tokens": 2500
  }'

# 2. Inspect monthly usage rollup & integer cost calculation
curl -s "http://localhost:8000/usage?tenant_id=tenant_free"

# 3. Create a Stripe test-mode Checkout Session to upgrade Free -> Pro
curl -s -X POST http://localhost:8000/billing/checkout \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "tenant_free", "plan_id": "pro"}'

# 4. Trigger background Stripe reconciliation & quota alert worker
curl -s -X POST http://localhost:8000/jobs/reconcile \
  -H "Content-Type: application/json" \
  -d '{}'
```

---

## 6. Honest Limitations & Scope Notes

1. **No mid-cycle proration or automated overage invoicing**: In keeping with Section 7 ("Realistic scope — where to stop"), tenants exceeding their monthly quota are hard-blocked at the boundary with `429 Too Many Requests` (or `402 Payment Required` when their spend budget or subscription status requires payment) rather than accumulating unbounded post-paid overage invoices.
2. **Billing period granularity**: Usage rollups are bucketed by UTC calendar month (`YYYY-MM`) rather than per-tenant rolling anchor dates.
3. **Background worker execution**: Background jobs (`usage_alert_evaluation` and `stripe_reconciliation`) run via FastAPI `BackgroundTasks` and on-demand via `POST /jobs/reconcile` with bounded retries and failure alerting, rather than requiring a separate Redis/Celery broker container.
