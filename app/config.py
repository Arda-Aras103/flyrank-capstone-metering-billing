"""Application configuration and pinned pricing/quota constants.

All monetary values are represented as integers (micro-cents or cents), never floats.
1 USD = 100 cents = 1,000,000 micro-cents (1 cent = 10,000 micro-cents).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized environment settings and pinned billing constants."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    port: int = 8000
    app_base_url: str = "http://localhost:8000"

    # Persistence
    database_url: str = "sqlite:///./metering_billing.db"

    # Stripe (Test Mode only - loaded exclusively from environment)
    stripe_secret_key: str = "sk_test_placeholder_replace_in_env"
    stripe_webhook_secret: str = "whsec_placeholder_replace_in_env"
    stripe_pro_price_id: str = "price_test_pro_plan_placeholder"

    # Pinned Plan Monthly Quotas
    free_plan_api_calls_limit: int = 1_000
    free_plan_ai_tokens_limit: int = 100_000
    pro_plan_api_calls_limit: int = 50_000
    pro_plan_ai_tokens_limit: int = 5_000_000

    # Pinned Plan Base Monthly Subscription Prices (in integer cents)
    free_plan_base_price_cents: int = 0
    pro_plan_base_price_cents: int = 4_900  # $49.00 / month

    # Pinned Usage Pricing Constants (in integer micro-cents)
    # 1 cent = 10,000 micro-cents; $1.00 = 1,000,000 micro-cents
    # - API Call: 1,000 micro-cents per call ($0.001 / call = $1.00 per 1,000 calls)
    # - Fresh input token: 2 micro-cents per token ($2.00 / 1M tokens)
    # - Cached input token: 1 micro-cent per token ($1.00 / 1M tokens, 50% cheaper than fresh input)
    # - Output token: 8 micro-cents per token ($8.00 / 1M tokens)
    # - Reasoning token: billed at the output token rate (8 micro-cents per token)
    price_per_api_call_microcents: int = 1_000
    price_input_token_microcents: int = 2
    price_cached_input_token_microcents: int = 1
    price_output_token_microcents: int = 8

    # Background Job & Alert Thresholds
    reconciliation_max_retries: int = 3
    usage_alert_warn_threshold_pct: int = 80
    usage_alert_limit_threshold_pct: int = 100


settings = Settings()
