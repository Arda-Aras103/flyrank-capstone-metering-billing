-- 001_initial_schema.sql
-- Usage Metering & Billing Engine Schema Migration
-- All monetary quantities are stored as integers (cents or microcents), never floats.

CREATE TABLE IF NOT EXISTS plans (
    id VARCHAR(32) PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    api_calls_limit INTEGER NOT NULL,
    ai_tokens_limit INTEGER NOT NULL,
    base_price_cents INTEGER NOT NULL DEFAULT 0,
    stripe_price_id VARCHAR(128),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tenants (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    email VARCHAR(255) NOT NULL,
    plan_id VARCHAR(32) NOT NULL REFERENCES plans(id),
    subscription_status VARCHAR(32) NOT NULL DEFAULT 'active',
    stripe_customer_id VARCHAR(128) UNIQUE,
    stripe_subscription_id VARCHAR(128) UNIQUE,
    budget_limit_microcents BIGINT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tenants_plan_id ON tenants(plan_id);
CREATE INDEX IF NOT EXISTS idx_tenants_stripe_customer ON tenants(stripe_customer_id);

CREATE TABLE IF NOT EXISTS subscriptions (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL REFERENCES tenants(id),
    stripe_subscription_id VARCHAR(128) UNIQUE NOT NULL,
    stripe_customer_id VARCHAR(128) NOT NULL,
    plan_id VARCHAR(32) NOT NULL REFERENCES plans(id),
    status VARCHAR(32) NOT NULL,
    current_period_start TIMESTAMP,
    current_period_end TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_tenant_id ON subscriptions(tenant_id);

CREATE TABLE IF NOT EXISTS usage_events (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL REFERENCES tenants(id),
    idempotency_key VARCHAR(128) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    usage_type VARCHAR(32) NOT NULL,
    quantity INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cost_microcents BIGINT NOT NULL DEFAULT 0,
    billing_period VARCHAR(7) NOT NULL,
    response_snapshot TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_tenant_idempotency UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_usage_events_tenant_period_type
    ON usage_events(tenant_id, billing_period, usage_type);

CREATE TABLE IF NOT EXISTS webhook_events (
    stripe_event_id VARCHAR(128) PRIMARY KEY,
    event_type VARCHAR(128) NOT NULL,
    tenant_id VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'processed',
    payload_hash VARCHAR(64) NOT NULL,
    processed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS usage_alerts (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL REFERENCES tenants(id),
    billing_period VARCHAR(7) NOT NULL,
    usage_type VARCHAR(32) NOT NULL,
    threshold_pct INTEGER NOT NULL,
    current_used INTEGER NOT NULL,
    plan_limit INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_tenant_period_type_threshold UNIQUE (tenant_id, billing_period, usage_type, threshold_pct)
);

CREATE INDEX IF NOT EXISTS idx_usage_alerts_tenant_id ON usage_alerts(tenant_id);

CREATE TABLE IF NOT EXISTS background_jobs (
    id VARCHAR(64) PRIMARY KEY,
    job_type VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64),
    status VARCHAR(32) NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    max_retries INTEGER NOT NULL DEFAULT 3,
    details TEXT,
    failure_alert TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_background_jobs_type_status ON background_jobs(job_type, status);
