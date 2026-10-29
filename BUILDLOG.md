# BUILDLOG.md — AI-Assisted Engineering Log

This log documents how AI tools were used during the development of the **Usage Metering & Billing Engine**, where AI-generated code initially failed or had subtle bugs, and what architectural and code changes were made to ensure correctness.

---

## 1. Where AI Helped Accelerate Development

1. **Boilerplate & Schema Scaffolding (Phase 1)**
   - Used AI assistance to draft the SQL migration (`migrations/001_initial_schema.sql`) and SQLAlchemy 2.0 `Mapped` ORM models (`app/db/models.py`) covering `plans`, `tenants`, `subscriptions`, `usage_events`, `webhook_events`, `usage_alerts`, and `background_jobs`.
   - Generated Pydantic v2 boundary validation models (`app/schemas.py`) with `ConfigDict(extra="forbid")` and `@model_validator(mode="after")` so invalid inputs (negative token counts, mismatched token sums, unknown fields) return `422 Unprocessable Entity` before reaching the business layer.

2. **HMAC Signature Test Helpers (Phase 3 & 4)**
   - Assisted in writing `generate_stripe_signature` in `tests/test_acceptance_probes.py` to generate real `t=<timestamp>,v1=<hmac_sha256>` headers verified by `stripe.Webhook.construct_event` without needing external network calls during automated CI/probe runs.

---

## 2. Where AI Was Wrong & What Was Changed

### Bug #1: `stripe` Python SDK v16 `Event` Object Compatibility
* **What happened:** In `app/services/stripe_service.py`, the initial implementation treated the return value of `stripe.Webhook.construct_event(payload=raw_body, sig_header=sig_header, secret=...)` as a standard Python `dict` and called `event.get("data", {}).get("object", {})`.
* **How it failed:** Under `stripe>=11` (specifically `stripe 16.0.1`), `construct_event` returns a `stripe.Event` instance (`StripeObject`), which raises:
  ```text
  AttributeError: 'get' is a dict method, but a Event is not a dict. Use .to_dict() to convert it.
  ```
* **What we changed:** Updated `StripeService.process_webhook` (`app/services/stripe_service.py`) to explicitly normalize the verified `Event` object via:
  ```python
  event_dict = event.to_dict() if hasattr(event, "to_dict") else dict(event)
  ```
  before extracting `id`, `type`, and `data.object`.

### Bug #2: Background Task Database Session Isolation in Tests
* **What happened:** When `POST /generate` recorded a usage event, it scheduled `_run_usage_alert_check_in_background(payload.tenant_id)` via FastAPI `BackgroundTasks`. The initial implementation opened a session using the global `SessionLocal()` inside the background worker.
* **How it failed:** During `pytest` runs (`test_shared_requirements_validation_tenant_isolation_and_background_jobs`), the HTTP request used an isolated in-memory SQLite engine injected via `app.dependency_overrides[get_db]`, whereas `SessionLocal()` connected to the file-based SQLite database. As a result, the background alert task looked for the tenant in the wrong database and failed after 3 retries (`Tenant 'tenant_budget_guarded' not found during alert evaluation`).
* **What we changed:** Passed `db.get_bind()` from the active request session into `_run_usage_alert_check_in_background(tenant_id, db_bind)` in `app/api/routes.py` and bound a dedicated `sessionmaker(bind=db_bind)` for the background execution. This keeps the background job on a separate session lifecycle from the request while targeting the exact same database engine in both production and test environments.

### Bug #3: Avoiding Sub-Cent Integer Division Truncation in Token Pricing
* **What happened:** An early design draft priced AI tokens per 1,000 tokens (`200` micro-cents per 1,000 tokens) using integer floor division (`tokens * rate // 1000`).
* **Why we changed it:** If a tenant made a small request of 3 tokens, integer floor division (`3 * 50 // 1000`) would truncate the cost to `0` micro-cents, allowing "micro-slicing" attacks where many tiny requests bypass cost accumulation.
* **What we changed:** We pinned integer per-token micro-cent rates (`PRICE_INPUT_TOKEN_MICROCENTS = 2`, `PRICE_CACHED_INPUT_TOKEN_MICROCENTS = 1`, `PRICE_OUTPUT_TOKEN_MICROCENTS = 8`, where `$1.00 = 1,000,000` micro-cents). Every single token multiplies by an exact integer with zero division, zero floating-point representation, and zero truncation drift.

### Bug #4: Typo in Custom Exception Definition (`classBillingError`)
* **What happened:** A missing space in `classBillingError(Exception):` in `app/services/meter_service.py` was caught immediately during code review before test execution and fixed to `class BillingError(Exception):`.

---

## 3. Code Ownership & Verification Summary
Every line of the repository is structured across three explicit layers:
1. **HTTP Layer (`app/api/routes.py`)**: FastAPI route handlers, header extraction (`Idempotency-Key`, `Stripe-Signature`), and raw-body webhook capture.
2. **Service Layer (`app/services/`)**: `MeterService` (idempotency + quota checks), `PricingService` (integer micro-cent arithmetic), `StripeService` (Checkout + webhook verification/deduplication), and `BackgroundJobService` (80%/100% alerts + Stripe reconciliation with retries).
3. **Repository / Data Layer (`app/repositories/billing_repository.py` & `app/db/models.py`)**: Tenant-isolated SQLAlchemy queries backed by `migrations/001_initial_schema.sql`.
