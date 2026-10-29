# Phase 1 · One-Page Design Document: Usage Metering & Billing Engine

## 1. Problem Statement
Every multi-tenant SaaS application must reliably answer three questions for every tenant:
1. **How much has this tenant used?** (Exactly-once usage metering under client network retries)
2. **Have they reached their plan quota?** (Deterministic boundary enforcement before executing billable work)
3. **How much does their usage cost?** (Exact integer money math encoding distinct AI token pricing rules, synchronized with Stripe subscriptions via verified, idempotent webhooks)

A single bug in this path causes double-charging on retries, quota leakage, floating-point rounding drift, or unauthorized plan upgrades from forged webhooks.

---

## 2. Data Model & Isolation
All customer data is strictly isolated by `tenant_id`. All monetary values are stored and calculated as **integers** (`microcents`, where `10,000 microcents = 1 cent` and `1,000,000 microcents = $1.00 USD`), never floats.

* **`plans`**: Defines subscription tiers (`free`, `pro`), monthly quotas (`api_calls_limit`, `ai_tokens_limit`), and base monthly price (`base_price_cents`).
  * **Free Plan**: `1,000` API calls / month, `100,000` AI tokens / month (`$0/mo`).
  * **Pro Plan**: `50,000` API calls / month, `5,000,000` AI tokens / month (`$49/mo` = `4900` cents).
* **`tenants`**: Customer organization (`id`, `name`, `email`, `plan_id`, `subscription_status` [`active`, `past_due`, `unpaid`, `canceled`], `stripe_customer_id`, `budget_limit_microcents`).
* **`subscriptions`**: Mirrors Stripe subscription state (`id`, `tenant_id`, `stripe_subscription_id`, `stripe_customer_id`, `plan_id`, `status`, `current_period_start`, `current_period_end`).
* **`usage_events`**: Append-only ledger of billable activity (`id`, `tenant_id`, `idempotency_key`, `usage_type` [`api_call` | `ai_tokens`], `quantity`, `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_tokens`, `cost_microcents`, `billing_period`, `response_snapshot`, `created_at`).
  * Enforces `UNIQUE (tenant_id, idempotency_key)` at the database constraint level.
* **`webhook_events`**: Deduplication table for Stripe webhooks (`stripe_event_id` PK, `event_type`, `status`, `processed_at`).
* **`background_jobs` & `usage_alerts`**: Tracks asynchronous threshold alerts (80% / 100% quota) and nightly Stripe-to-DB state reconciliation jobs with retry counts and failure alerts.

---

## 3. API Surface & Idempotency / Boundary Contract

| Method & Path | Purpose | Key Contract |
| :--- | :--- | :--- |
| `POST /generate` | Dummy billable endpoint (records API call or AI tokens) | Requires `Idempotency-Key` header (or body field). Checks plan status & quota **before** recording. |
| `POST /meter` | Explicit metering endpoint | Alias/direct metering endpoint supporting identical idempotency & quota rules. |
| `GET /usage` | Monthly usage & cost rollup | Query `tenant_id` (or `X-Tenant-Id`). Returns `used`, `limit`, `remaining`, and exact `cost` breakdown. |
| `POST /billing/checkout` | Stripe Checkout Session (test mode) | Creates a Stripe Checkout session to upgrade a tenant from `free` to `pro`. |
| `POST /webhooks/stripe` | Stripe webhook receiver | Verifies `Stripe-Signature` against raw payload (`400` on forgery), deduplicates by `event.id`, updates plan/status. |
| `POST /jobs/reconcile` | Trigger background reconciliation / alert worker | Executes async reconciliation & alert evaluation with retries and failure alerting. |

### Idempotency Strategy
1. Client sends `Idempotency-Key` with `POST /generate`.
2. `MeterService` looks up `(tenant_id, idempotency_key)` inside a transaction.
3. If found and the payload fingerprint matches, the original stored response is returned immediately (`idempotent_replay: true`) without inserting a second `usage_event` or incrementing usage.
4. If the same `Idempotency-Key` is reused with a different request payload, the service returns `409 Conflict`.

### Quota Boundary & Status Code Semantics (`429` vs `402`)
* **`402 Payment Required`**: Returned when the tenant's subscription status is `past_due`, `unpaid`, or `canceled`, OR when the tenant's configured monthly spend budget (`budget_limit_microcents`) would be exceeded, OR when a Free-tier tenant attempts a Pro-only action/upgrade boundary requiring payment.
* **`429 Too Many Requests`**: Returned (with `Retry-After` header and clear explanation) when an active tenant's `current_usage + requested_quantity > plan_limit`.
  * **Boundary rule**: At `999 / 1,000` calls, a request for `1` call brings total to `1,000 / 1,000` -> **Allowed (`200 OK`)**. The next request (`1,000 + 1 > 1,000`) -> **Rejected (`429 Too Many Requests`)**.

---

## 4. Layered Architecture Sketch
```text
HTTP Layer (app/api/routes.py)        --> Pydantic boundary validation (422/400 on bad input), raw body webhook capture
Service Layer (app/services/*.py)     --> MeterService, PricingService, StripeService, BackgroundJobService
Data Layer (app/repositories/*.py)    --> TenantRepo, UsageRepo, WebhookRepo (SQLAlchemy ORM + SQL migrations)
Database (SQLite / PostgreSQL)        --> ACID transactions, UNIQUE(tenant_id, idempotency_key), tenant indexes
```

---

## 5. Explicit Non-Goal
**No mid-cycle proration or automated credit-card dunning emails in core:** We rely strictly on Stripe Checkout and Stripe's webhook lifecycle (`checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`) as the single source of payment truth rather than building a custom recurring charge scheduler.
