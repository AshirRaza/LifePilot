"""Shared evaluation cost helpers (no LLM calls)."""

from __future__ import annotations

from typing import Any


def estimate_usage_cost_usd(
    usage: dict[str, Any] | None,
    *,
    input_cost_per_mtok: float = 2.0,
    output_cost_per_mtok: float = 10.0,
) -> float:
    """Estimate USD cost from token usage dicts produced by AgentResponse.usage."""
    if not usage:
        return 0.0
    inp = float(usage.get("input_tokens") or 0)
    out = float(usage.get("output_tokens") or 0)
    return round(
        (inp / 1_000_000.0) * float(input_cost_per_mtok)
        + (out / 1_000_000.0) * float(output_cost_per_mtok),
        6,
    )
