"""Token price constants for cost calculation."""

from decimal import Decimal

# Prices per 1M tokens. Updated 2026-05-10 from vendor docs.
COST_RATES: dict[str, dict[str, dict[str, Decimal]]] = {
    "anthropic": {
        "claude-sonnet-4-6": {
            "input":  Decimal("3.00"),   # $3/1M input tokens
            "output": Decimal("15.00"),  # $15/1M output tokens
        },
        "claude-haiku-4-5-20251001": {
            "input":  Decimal("0.80"),   # $0.80/1M input tokens
            "output": Decimal("4.00"),   # $4/1M output tokens
        },
    },
    "openai": {
        "text-embedding-3-small": {
            "input":  Decimal("0.02"),   # $0.02/1M tokens (embeddings are input-only)
            "output": Decimal("0.00"),   # embeddings have no output tokens
        },
    },
}


def get_cost(provider: str, model: str, direction: str, tokens: int) -> Decimal:
    """Return cost in USD for a given provider/model/direction/tokens."""
    rate = COST_RATES[provider][model][direction]
    return (Decimal(tokens) / Decimal(1_000_000)) * rate
