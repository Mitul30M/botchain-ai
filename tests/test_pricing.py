"""Unit tests for per-model token pricing -> messages.credits_cost."""

from decimal import Decimal

import pytest

import app.services.pricing as pricing_mod
from app.services.pricing import (
    MODEL_PRICING,
    credits_cost,
    input_rate_per_million,
    output_rate_per_million,
)

MISTRAL = "ministral-14b-latest"


@pytest.fixture(autouse=True)
def _clear_pricing_env(monkeypatch):
    for name in list(pricing_mod.os.environ):
        if name.startswith("PRICING_"):
            monkeypatch.delenv(name, raising=False)


def test_table_entry_used_for_known_model():
    assert input_rate_per_million(MISTRAL) == MODEL_PRICING[MISTRAL].input_per_million
    assert output_rate_per_million(MISTRAL) == MODEL_PRICING[MISTRAL].output_per_million


def test_cost_is_weighted_input_plus_output():
    # 1M input at $0.20/M = $0.20; 1M output at $0.20/M = $0.20.
    assert credits_cost(1_000_000, 1_000_000, MISTRAL) == Decimal("0.400000")


def test_partial_token_counts():
    # 350k in + 25k out at $0.20/M each = 0.070 + 0.005
    cost = credits_cost(350_000, 25_000, MISTRAL)
    assert cost == Decimal("0.075000")


def test_no_usage_returns_none_not_zero():
    assert credits_cost(None, None, MISTRAL) is None
    assert credits_cost(0, 0, MISTRAL) is None


def test_zero_only_output_is_priced():
    assert credits_cost(None, 1_000_000, MISTRAL) == Decimal("0.200000")


def test_unknown_model_uses_settings_fallback(monkeypatch):
    monkeypatch.setattr(
        pricing_mod,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "pricing_unknown_input_per_million": Decimal("1.00"),
                "pricing_unknown_output_per_million": Decimal("3.00"),
            },
        )(),
    )
    assert credits_cost(1_000_000, 1_000_000, "some-future-model") == Decimal("4.000000")


def test_missing_model_name_uses_fallback():
    assert credits_cost(1_000_000, 0, None) is not None


def test_env_override_wins_over_table(monkeypatch):
    monkeypatch.setenv("PRICING_INPUT_PER_MILLION_MINISTRAL_14B_LATEST", "0.5")
    assert input_rate_per_million(MISTRAL) == Decimal("0.5")
    # Unspecified side keeps the table value.
    assert output_rate_per_million(MISTRAL) == MODEL_PRICING[MISTRAL].output_per_million


def test_env_override_can_introduce_unknown_model(monkeypatch):
    monkeypatch.setenv("PRICING_INPUT_PER_MILLION_BRAND_NEW", "2.5")
    assert input_rate_per_million("brand-new") == Decimal("2.5")


def test_malformed_env_override_falls_back_to_table(monkeypatch):
    monkeypatch.setenv("PRICING_INPUT_PER_MILLION_MINISTRAL_14B_LATEST", "not-a-number")
    assert input_rate_per_million(MISTRAL) == MODEL_PRICING[MISTRAL].input_per_million


def test_blank_env_override_falls_back_to_table(monkeypatch):
    monkeypatch.setenv("PRICING_INPUT_PER_MILLION_MINISTRAL_14B_LATEST", "  ")
    assert input_rate_per_million(MISTRAL) == MODEL_PRICING[MISTRAL].input_per_million


def test_cost_rounds_to_six_places():
    # 1 token of a $0.04/M model is 4e-8, which must not be lost entirely.
    cost = credits_cost(1, 0, "ministral-3b-latest")
    assert cost == Decimal("0.000000")


def test_slug_collapses_punctuation():
    assert pricing_mod._slug("ministral-14b-latest") == "MINISTRAL_14B_LATEST"
    assert pricing_mod._slug("gemma4:cloud") == "GEMMA4_CLOUD"


def test_all_table_rates_are_decimals():
    for name, price in MODEL_PRICING.items():
        assert isinstance(price.input_per_million, Decimal), name
        assert isinstance(price.output_per_million, Decimal), name
