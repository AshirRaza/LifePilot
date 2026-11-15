"""Evaluation helpers (offline and live comparative runs).

Production verification lives in ``lifepilot.agent.verification`` and is invoked
by the main agent graph — eval harnesses must not reimplement it.
"""

from lifepilot.eval.accounting import estimate_usage_cost_usd
from lifepilot.eval.comparative import (
    MODE_SPECS,
    MODES,
    ExternalOnlyEvidenceOrchestrator,
    classify_automated_assessment,
    retrieval_options_for_mode,
)

__all__ = [
    "MODE_SPECS",
    "MODES",
    "ExternalOnlyEvidenceOrchestrator",
    "classify_automated_assessment",
    "estimate_usage_cost_usd",
    "retrieval_options_for_mode",
]
