"""Pricing and integer money calculation service.

Rules enforced:
1. Store and compute money strictly as integers (micro-cents and cents), NEVER floats.
   - 1 cent = 10,000 micro-cents
   - $1.00 USD = 100 cents = 1,000,000 micro-cents
2. AI Token Pricing Rules:
   - Fresh input tokens are billed at `price_input_token_microcents` (2 micro-cents/token).
   - Cached input tokens are cheaper: billed at `price_cached_input_token_microcents` (1 micro-cent/token).
   - Reasoning tokens count as output tokens: `(output_tokens + reasoning_tokens) * price_output_token_microcents` (8 micro-cents/token).
   - Token categories CANNOT simply be added together before pricing.
"""

from app.config import settings


MICROCENTS_PER_CENT = 10_000
MICROCENTS_PER_USD = 1_000_000


class PricingService:
    """Stateless integer money calculator."""

    @staticmethod
    def format_microcents_to_usd(microcents: int) -> str:
        """Convert integer micro-cents to a deterministic USD string without floats."""
        sign = "-" if microcents < 0 else ""
        abs_mc = abs(int(microcents))
        dollars, remainder = divmod(abs_mc, MICROCENTS_PER_USD)
        # Format with 6 decimal places if sub-cent precision exists, or 2 decimal places if exact cents
        if remainder % MICROCENTS_PER_CENT == 0:
            cents = remainder // MICROCENTS_PER_CENT
            return f"{sign}${dollars}.{cents:02d}"
        return f"{sign}${dollars}.{remainder:06d}"

    @classmethod
    def microcents_to_cost_summary(cls, microcents: int) -> dict:
        """Return integer micro-cents, integer cents, and formatted USD string."""
        mc = int(microcents)
        return {
            "microcents": mc,
            "cents": mc // MICROCENTS_PER_CENT,
            "formatted_usd": cls.format_microcents_to_usd(mc),
        }

    @staticmethod
    def calculate_api_call_cost_microcents(quantity: int) -> int:
        """Calculate cost in integer micro-cents for N billable API calls."""
        if quantity < 0:
            raise ValueError("API call quantity cannot be negative")
        return int(quantity) * int(settings.price_per_api_call_microcents)

    @staticmethod
    def calculate_ai_tokens_cost_breakdown(
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
    ) -> dict[str, int]:
        """Calculate exact integer cost breakdown for AI token categories.

        - Cached input tokens are billed at the cheaper cached rate.
        - Reasoning tokens count as output tokens and are billed at the output rate.
        - Categories are priced independently and never summed before multiplication.
        """
        if any(t < 0 for t in (input_tokens, cached_input_tokens, output_tokens, reasoning_tokens)):
            raise ValueError("Token counts cannot be negative")

        in_tok = int(input_tokens)
        cached_tok = int(cached_input_tokens)
        out_tok = int(output_tokens)
        reason_tok = int(reasoning_tokens)

        effective_output_tokens = out_tok + reason_tok
        total_tokens = in_tok + cached_tok + effective_output_tokens

        input_cost_mc = in_tok * int(settings.price_input_token_microcents)
        cached_input_cost_mc = cached_tok * int(settings.price_cached_input_token_microcents)
        output_and_reasoning_cost_mc = effective_output_tokens * int(settings.price_output_token_microcents)
        total_cost_mc = input_cost_mc + cached_input_cost_mc + output_and_reasoning_cost_mc

        return {
            "input_tokens": in_tok,
            "cached_input_tokens": cached_tok,
            "output_tokens": out_tok,
            "reasoning_tokens": reason_tok,
            "effective_output_tokens": effective_output_tokens,
            "total_tokens": total_tokens,
            "input_cost_microcents": input_cost_mc,
            "cached_input_cost_microcents": cached_input_cost_mc,
            "output_and_reasoning_cost_microcents": output_and_reasoning_cost_mc,
            "total_token_cost_microcents": total_cost_mc,
        }

    @staticmethod
    def get_pinned_pricing_config() -> dict:
        """Expose pinned pricing constants for transparency and automated verification."""
        return {
            "currency": "USD",
            "unit": "integer_microcents (1 USD = 100 cents = 1,000,000 microcents)",
            "api_call_microcents": int(settings.price_per_api_call_microcents),
            "ai_tokens": {
                "input_token_microcents": int(settings.price_input_token_microcents),
                "cached_input_token_microcents": int(settings.price_cached_input_token_microcents),
                "output_token_microcents": int(settings.price_output_token_microcents),
                "reasoning_token_microcents": int(settings.price_output_token_microcents),
                "rules": [
                    "cached_input_tokens are cheaper than fresh input_tokens",
                    "reasoning_tokens count as output_tokens and are billed at the output rate",
                    "token categories are priced separately and never summed before multiplication",
                ],
            },
            "plans": {
                "free": {
                    "base_price_cents": int(settings.free_plan_base_price_cents),
                    "api_calls_limit": int(settings.free_plan_api_calls_limit),
                    "ai_tokens_limit": int(settings.free_plan_ai_tokens_limit),
                },
                "pro": {
                    "base_price_cents": int(settings.pro_plan_base_price_cents),
                    "api_calls_limit": int(settings.pro_plan_api_calls_limit),
                    "ai_tokens_limit": int(settings.pro_plan_ai_tokens_limit),
                },
            },
        }
