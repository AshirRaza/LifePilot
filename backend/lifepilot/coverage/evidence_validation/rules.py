"""Evidence-quality rules for Phase 6F.0 (shared with tests)."""

from __future__ import annotations

import re

NAV_ONLY_RE = re.compile(
    r"^(information for applicants|includes? |refer to the checklist|"
    r"see (the |our )?webpage|click here|learn more)\b",
    re.IGNORECASE,
)


def heading_alone_cannot_be_complete(
    *,
    heading: str | None,
    body: str | None,
    min_body_chars: int = 80,
) -> bool:
    """Return True when COMPLETE would be invalid (heading without substance)."""
    body_text = (body or "").strip()
    if not (heading or "").strip():
        return False
    if len(body_text) < min_body_chars:
        return True
    if NAV_ONLY_RE.match(body_text):
        return True
    return False


def unrelated_required_documents_snippet(excerpt: str) -> bool:
    """Detect known false-positive required_documents excerpts."""
    low = (excerpt or "").lower()
    if "up to 3 months before" in low and "certificate of sponsorship" in low:
        # Timing/CoS mention without a documents list
        if "you’ll need to provide" not in low and "documents you'll need" not in low:
            return True
    if low.strip() in {"passport", "your passport"}:
        return True
    return False


def pathway_cross_contamination(excerpt: str, pathway_id: str) -> bool:
    """Evidence naming a different pathway should not satisfy this one."""
    low = (excerpt or "").lower()
    if pathway_id == "nl_naturalisation":
        if (
            "application process for the residence permit" in low
            and "naturalisation" not in low
        ):
            return True
    if pathway_id.startswith("sg_") and "h-1b" in low:
        return True
    if pathway_id == "au_student_500" and "diplomatic" in low:
        return True
    return False


def unevaluated_must_not_become_missing_without_assessment(
    previous: str, proposed: str, *, assessed: bool
) -> bool:
    """Return True if the transition is illegal."""
    if previous == "UNEVALUATED" and proposed == "MISSING" and not assessed:
        return True
    return False


def not_applicable_requires_rationale(classification: str, rationale: str) -> bool:
    if classification != "NOT_APPLICABLE":
        return True
    return bool((rationale or "").strip())


def cannot_auto_assign_human_verified(reviewer_status: str) -> bool:
    return reviewer_status != "human_verified"
