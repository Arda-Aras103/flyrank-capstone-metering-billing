"""Pydantic request and response schemas for strict boundary validation."""

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class TokenBreakdown(BaseModel):
    """Granular AI token counts for accurate category-based pricing."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0, description="Fresh (non-cached) input tokens")
    cached_input_tokens: int = Field(default=0, ge=0, description="Cached input tokens (billed cheaper)")
    output_tokens: int = Field(default=0, ge=0, description="Standard generated output tokens")
    reasoning_tokens: int = Field(
        default=0, ge=0, description="Hidden thinking/reasoning tokens (billed at output token rate)"
    )

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cached_input_tokens
            + self.output_tokens
            + self.reasoning_tokens
        )


class BillableRequest(BaseModel):
    """Payload for POST /generate (dummy billable endpoint) or POST /meter."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(..., min_length=1, max_length=64, description="Tenant identifier")
    usage_type: Literal["api_call", "ai_tokens"] = Field(
        default="api_call", description="Type of billable resource consumed"
    )
    quantity: int | None = Field(
        default=None, ge=1, le=10_000_000, description="Units consumed (defaults to 1 for api_call or sum of tokens)"
    )
    input_tokens: int = Field(default=0, ge=0, le=10_000_000)
    cached_input_tokens: int = Field(default=0, ge=0, le=10_000_000)
    output_tokens: int = Field(default=0, ge=0, le=10_000_000)
    reasoning_tokens: int = Field(default=0, ge=0, le=10_000_000)
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Optional body fallback if Idempotency-Key header is not used",
    )
    prompt: str | None = Field(default=None, max_length=2000, description="Optional simulated prompt for /generate")
    require_pro: bool = Field(
        default=False, description="When true, requires Pro plan (returns 402 Payment Required on Free plan)"
    )

    @model_validator(mode="after")
    def validate_usage_quantities(self) -> "BillableRequest":
        token_sum = (
            self.input_tokens
            + self.cached_input_tokens
            + self.output_tokens
            + self.reasoning_tokens
        )
        if self.usage_type == "api_call":
            if token_sum > 0:
                raise ValueError("Token counts cannot be non-zero when usage_type is 'api_call'")
            if self.quantity is None:
                self.quantity = 1
        elif self.usage_type == "ai_tokens":
            if token_sum > 0:
                if self.quantity is not None and self.quantity != token_sum:
                    raise ValueError(
                        f"When token breakdown is provided, quantity ({self.quantity}) must equal "
                        f"the sum of token categories ({token_sum})"
                    )
                self.quantity = token_sum
            else:
                if self.quantity is None or self.quantity <= 0:
                    raise ValueError(
                        "For 'ai_tokens', provide positive token category counts or a positive 'quantity'"
                    )
                # Default unspecified breakdown to output_tokens (conservative) or input_tokens
                self.output_tokens = self.quantity
        return self


class CostSummary(BaseModel):
    """Integer money representation (never floats)."""

    microcents: int
    cents: int
    formatted_usd: str


class BillableResponse(BaseModel):
    """Response returned by POST /generate and POST /meter."""

    event_id: str
    tenant_id: str
    plan_id: str
    usage_type: str
    quantity_recorded: int
    idempotency_key: str
    idempotent_replay: bool
    billing_period: str
    quota: dict
    event_cost: CostSummary
    simulated_output: str | None = None
    created_at: datetime


class QuotaBucketSummary(BaseModel):
    used: int
    limit: int
    remaining: int
    utilization_pct: int


class TokenUsageRollup(BaseModel):
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    effective_output_tokens: int
    total_tokens: int
    input_cost_microcents: int
    cached_input_cost_microcents: int
    output_and_reasoning_cost_microcents: int
    total_token_cost_microcents: int


class UsageRollupResponse(BaseModel):
    """Response returned by GET /usage."""

    tenant_id: str
    tenant_name: str
    plan_id: str
    subscription_status: str
    billing_period: str
    api_calls: QuotaBucketSummary
    ai_tokens: QuotaBucketSummary
    token_breakdown: TokenUsageRollup
    cost: dict
    pricing_rules_pinned: dict
    alerts_triggered: list[dict]


class CheckoutRequest(BaseModel):
    """Request to create a Stripe Checkout Session for upgrading to Pro."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(..., min_length=1, max_length=64)
    plan_id: Literal["pro"] = Field(default="pro")
    success_url: str | None = Field(default=None)
    cancel_url: str | None = Field(default=None)


class TenantCreateRequest(BaseModel):
    """Request to create or reset a tenant for testing."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    email: str = Field(..., min_length=3, max_length=255)
    plan_id: Literal["free", "pro"] = Field(default="free")
    subscription_status: Literal["active", "past_due", "unpaid", "canceled"] = Field(default="active")
    budget_limit_microcents: int | None = Field(default=None, ge=0)
