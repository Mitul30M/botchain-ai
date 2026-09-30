"""Per-model token pricing used to populate ``messages.credits_cost``.

``credits_cost`` is a DOLLAR value: ``0.20`` means the message consumed $0.20 of
LLM spend. Rates are USD per 1,000,000 tokens, the unit providers publish, and
the arithmetic runs in :class:`~decimal.Decimal` so costs are exact.

Resolution order for a model name:

1. env override — ``PRICING_INPUT_PER_MILLION_<SLUG>`` /
   ``PRICING_OUTPUT_PER_MILLION_<SLUG>``, where ``<SLUG>`` is the uppercased
   model name with every non-alphanumeric run collapsed to ``_``
   (``ministral-14b-latest`` -> ``MINISTRAL_14B_LATEST``). Lets a price change
   ship without a code change, and corrects a stale table entry.
2. :data:`MODEL_PRICING` below.
3. ``pricing_unknown_input_per_million`` / ``pricing_unknown_output_per_million``
   from settings, so a newly released model still records a real cost instead of
   silently pricing at zero.

The table holds provider *list* prices and goes stale. Verify against the
provider's pricing page before trusting a figure, or override it via env.
"""

import os
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.config import get_settings

# USD per 1,000,000 tokens.
RATE_UNIT = Decimal(1_000_000)

# Six decimal places: well below a cent, and stable for repeated runs.
PRECISION = Decimal("0.000001")


@dataclass(frozen=True)
class ModelPrice:
    """Input/output rate for one model, in USD per 1,000,000 tokens."""

    input_per_million: Decimal
    output_per_million: Decimal


MODEL_PRICING: dict[str, ModelPrice] = {
    "ministral-3b-latest": ModelPrice(Decimal("0.04"), Decimal("0.04")),
    "ministral-8b-latest": ModelPrice(Decimal("0.10"), Decimal("0.10")),
    "ministral-14b-latest": ModelPrice(Decimal("0.20"), Decimal("0.20")),
    "open-mistral-nemo": ModelPrice(Decimal("0.15"), Decimal("0.15")),
    "mistral-small-latest": ModelPrice(Decimal("0.20"), Decimal("0.60")),
    "gemma4:cloud": ModelPrice(Decimal("0.00"), Decimal("0.00")),
}


def _slug(model: str) -> str:
    """Return the env-var slug for a model name."""
    return re.sub(r"[^A-Z0-9]+", "_", model.upper()).strip("_")


def _env_decimal(name: str) -> Decimal | None:
    """Return a Decimal from an env var, or None when unset/unparseable."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return Decimal(raw.strip())
    except ArithmeticError:
        return None


def _price_for(model: str | None) -> ModelPrice:
    """Resolve the effective price for a model name."""
    settings = get_settings()
    fallback = ModelPrice(
        settings.pricing_unknown_input_per_million,
        settings.pricing_unknown_output_per_million,
    )
    if not model:
        return fallback

    slug = _slug(model)
    env_in = _env_decimal(f"PRICING_INPUT_PER_MILLION_{slug}")
    env_out = _env_decimal(f"PRICING_OUTPUT_PER_MILLION_{slug}")
    if env_in is not None or env_out is not None:
        base = MODEL_PRICING.get(model, fallback)
        return ModelPrice(
            env_in if env_in is not None else base.input_per_million,
            env_out if env_out is not None else base.output_per_million,
        )

    return MODEL_PRICING.get(model, fallback)


def input_rate_per_million(model: str | None) -> Decimal:
    """Return the USD-per-1M-token input rate for a model."""
    return _price_for(model).input_per_million


def output_rate_per_million(model: str | None) -> Decimal:
    """Return the USD-per-1M-token output rate for a model."""
    return _price_for(model).output_per_million


def credits_cost(
    input_tokens: int | None,
    output_tokens: int | None,
    model: str | None,
) -> Decimal | None:
    """Return the dollar cost of one assistant message, or None if unpriceable.

    Returns None when there is no token usage at all, so an unrun/failed message
    stays NULL rather than recording a misleading 0.
    """
    if not input_tokens and not output_tokens:
        return None

    price = _price_for(model)
    total = (Decimal(input_tokens or 0) * price.input_per_million) / RATE_UNIT
    total += (Decimal(output_tokens or 0) * price.output_per_million) / RATE_UNIT
    return total.quantize(PRECISION, rounding=ROUND_HALF_UP)
