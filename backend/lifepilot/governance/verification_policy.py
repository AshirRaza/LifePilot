"""Server-controlled legal verification policy (Phase 10D.4).

Callers cannot downgrade an enforced policy via request parameters.
"""

from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache

from lifepilot.agent.evidence import EvidenceItem, EvidenceVerificationState
from lifepilot.agent.retrieval_policy import detect_high_risk_categories


class LegalVerificationPolicy(str, Enum):
    ADVISORY = "advisory"
    STRICT_VERIFIED_ONLY = "strict_verified_only"


VALID_POLICIES = frozenset(p.value for p in LegalVerificationPolicy)


@lru_cache(maxsize=1)
def get_legal_verification_policy() -> LegalVerificationPolicy:
    raw = (os.environ.get("LIFEPILOT_LEGAL_VERIFICATION_POLICY") or "advisory").strip().lower()
    if raw not in VALID_POLICIES:
        return LegalVerificationPolicy.ADVISORY
    return LegalVerificationPolicy(raw)


def reset_legal_verification_policy_cache() -> None:
    get_legal_verification_policy.cache_clear()


def resolve_effective_policy(
    *,
    requested: str | None = None,
    allow_client_override: bool = False,
) -> LegalVerificationPolicy:
    """Resolve policy. Client override is ignored unless explicitly allowed (never for API)."""
    base = get_legal_verification_policy()
    if not allow_client_override or not requested:
        return base
    cleaned = (requested or "").strip().lower()
    if cleaned not in VALID_POLICIES:
        return base
    # Never allow downgrading strict → advisory via client.
    if base == LegalVerificationPolicy.STRICT_VERIFIED_ONLY:
        return base
    return LegalVerificationPolicy(cleaned)


def has_human_verified_rule_evidence(evidence: list[EvidenceItem]) -> bool:
    return any(
        e.verification_state == EvidenceVerificationState.HUMAN_VERIFIED_RULE
        for e in evidence
    )


def strict_mode_blocks_high_risk_claim(
    *,
    question: str,
    claim_text: str,
    evidence: list[EvidenceItem],
    policy: LegalVerificationPolicy | None = None,
) -> tuple[bool, str | None]:
    """Under strict policy, high-risk normative claims need human-verified rule evidence."""
    pol = policy or get_legal_verification_policy()
    if pol != LegalVerificationPolicy.STRICT_VERIFIED_ONLY:
        return False, None
    cats = detect_high_risk_categories(f"{question}\n{claim_text}")
    if not cats:
        return False, None
    if has_human_verified_rule_evidence(evidence):
        return False, None
    return True, f"strict_verified_only_missing_human_rule:{','.join(cats)}"


def claim_human_legal_label(
    *,
    evidence_ids: list[str],
    evidence: list[EvidenceItem],
) -> str:
    """Public-facing label: never equate mechanical SUPPORT with human approval."""
    by_id = {e.evidence_id: e for e in evidence}
    for eid in evidence_ids:
        item = by_id.get(eid)
        if item is not None and item.verification_state == EvidenceVerificationState.HUMAN_VERIFIED_RULE:
            return "human_verified_rule"
    return "mechanically_supported_or_unverified"
