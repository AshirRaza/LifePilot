"""Evaluation helpers (offline and live comparative runs)."""

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
    "retrieval_options_for_mode",
]
