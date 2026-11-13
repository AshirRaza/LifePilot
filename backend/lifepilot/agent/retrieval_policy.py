"""Automatic hybrid retrieval decisions for the main LifePilot agent.

One coherent policy: curated internal evidence by default; official external
verification when risk, freshness, thin evidence, or conflicts warrant it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from lifepilot.agent.evidence import EvidenceItem

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FRESHNESS_POLICY = ROOT / "config" / "freshness" / "source_freshness_policy.json"
DEFAULT_REVIEW_QUEUE = ROOT / "config" / "eval" / "high_risk_legal_review_queue.json"


class RetrievalDecision(str, Enum):
    INTERNAL_ONLY = "internal_only"
    INTERNAL_PLUS_EXTERNAL = "internal_plus_external"
    EXTERNAL_VERIFICATION_REQUIRED = "external_verification_required"


class ExternalMode(str, Enum):
    AUTO = "auto"
    OFF = "off"
    ON = "on"


_HIGH_RISK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("salary_threshold", re.compile(
        r"(?i)\b(salary|salaries|remuneration|wage|going rate|qualifying salary|"
        r"minimum salary|income threshold|financial requirement|maintenance|"
        r"proof of funds|settlement funds)\b"
    )),
    ("application_fee", re.compile(
        r"(?i)\b(application fee|visa fee|filing fee|immigration health surcharge|"
        r"ihs\b|how much (does|do|is)|cost of (the )?(visa|permit|application))\b"
    )),
    ("eligibility", re.compile(
        r"(?i)\b(eligib|qualify|qualification|requirement|criteria|exempt|"
        r"exemption|work (right|authori[sz]ation)|permanent residence|"
        r"indefinite leave|settlement)\b"
    )),
    ("regulatory_change", re.compile(
        r"(?i)\b(current|currently|as of today|recent(ly)? changed|new rules?|"
        r"updated|update|effective (from|date)|transitional|from \d{1,2} |"
        r"after \d{1,2} )\b"
    )),
]

_FUTURE_EFFECTIVE_RE = re.compile(
    r"(?i)\b(effective|comes? into (force|effect)|applies? from|from)\s+"
    r"(\d{1,2}\s+\w+\s+20\d{2}|\w+\s+20\d{2}|20\d{2}-\d{2}-\d{2})"
)
_YEAR_RE = re.compile(r"(20\d{2})")
_HISTORICAL_RE = re.compile(
    r"(?i)\b(in 20\d{2}|as of 20\d{2}|historical|used to be|before 20\d{2}|"
    r"what was the|previously required)\b"
)


@dataclass(frozen=True)
class FreshnessAssessment:
    stale: bool
    unknown_age: bool
    reasons: tuple[str, ...] = ()
    matched_category: str | None = None
    age_hours: float | None = None
    recheck_hours: float | None = None


@dataclass(frozen=True)
class RetrievalPolicyDecision:
    decision: RetrievalDecision
    reasons: tuple[str, ...]
    high_risk_categories: tuple[str, ...] = ()
    freshness: FreshnessAssessment | None = None
    run_external: bool = False
    require_verification: bool = False


@dataclass
class ReconciliationNotes:
    warnings: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    future_effective_mentions: list[str] = field(default_factory=list)


@lru_cache(maxsize=1)
def load_freshness_policy(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_FRESHNESS_POLICY
    if not p.exists():
        return {"categories": {}, "priority_sources": []}
    return json.loads(p.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_review_queue(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_REVIEW_QUEUE
    if not p.exists():
        return {"items": []}
    return json.loads(p.read_text(encoding="utf-8"))


def detect_high_risk_categories(question: str) -> list[str]:
    text = question or ""
    found: list[str] = []
    for name, pattern in _HIGH_RISK_PATTERNS:
        if pattern.search(text):
            found.append(name)
    return found


def review_queue_uncertainty(
    *,
    jurisdiction: str,
    pathway_id: str | None,
) -> list[str]:
    jur = (jurisdiction or "").strip().upper()
    pid = (pathway_id or "").strip() or None
    reasons: list[str] = []
    for item in load_review_queue().get("items") or []:
        if (item.get("jurisdiction") or "").upper() != jur:
            continue
        item_pid = (item.get("pathway_id") or "").strip() or None
        # Pathway-scoped queue items only apply when the request names that pathway.
        if item_pid and item_pid != pid:
            continue
        status = (item.get("verification_status") or "").lower()
        if status in {
            "requires_human_review",
            "conflicting_evidence",
            "insufficient_evidence",
            "potentially_outdated",
        }:
            reasons.append(f"review_queue:{item.get('id')}:{status}")
    try:
        from lifepilot.maintenance.pending_changes import pending_uncertainty_reasons

        reasons.extend(
            pending_uncertainty_reasons(jurisdiction=jur, pathway_id=pid)
        )
    except Exception:  # noqa: BLE001
        pass
    return reasons


def classify_url_freshness_category(url: str, policy: dict[str, Any] | None = None) -> str | None:
    blob = (url or "").lower()
    pol = policy or load_freshness_policy()
    for src in pol.get("priority_sources") or []:
        needle = (src.get("url_contains") or "").lower()
        if needle and needle in blob:
            return src.get("category")
    return None


def assess_internal_freshness(
    *,
    urls: Iterable[str],
    version_retrieved_at: dict[int, datetime] | None = None,
    version_ids: Iterable[int] | None = None,
    question_categories: Iterable[str] | None = None,
    policy: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> FreshnessAssessment:
    """Evaluate freshness of retrieved internal evidence (not whole-corpus age)."""
    pol = policy or load_freshness_policy()
    categories = pol.get("categories") or {}
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    matched_category: str | None = None
    recheck_hours: float | None = None
    ages: list[float] = []

    url_list = [u for u in urls if u]
    for url in url_list:
        cat = classify_url_freshness_category(url, pol)
        if cat:
            matched_category = matched_category or cat
            limit = float((categories.get(cat) or {}).get("recheck_hours") or 336)
            recheck_hours = limit if recheck_hours is None else min(recheck_hours, limit)

    # Conservative: high-risk salary/fee questions without matched URL still use tight window.
    qcats = set(question_categories or [])
    if matched_category is None and (
        "salary_threshold" in qcats or "application_fee" in qcats
    ):
        matched_category = (
            "salary_thresholds_financial"
            if "salary_threshold" in qcats
            else "application_fees"
        )
        recheck_hours = float(
            (categories.get(matched_category) or {}).get("recheck_hours") or 24
        )
        reasons.append("conservative_category_from_question_risk")

    if version_retrieved_at and version_ids:
        for vid in version_ids:
            dt = version_retrieved_at.get(int(vid))
            if dt is None:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ages.append((now - dt.astimezone(timezone.utc)).total_seconds() / 3600.0)

    if not ages:
        # Unknown age for matched high-risk / priority evidence → treat conservatively.
        if matched_category and recheck_hours is not None:
            reasons.append("unknown_evidence_age")
            return FreshnessAssessment(
                stale=True,
                unknown_age=True,
                reasons=tuple(reasons),
                matched_category=matched_category,
                age_hours=None,
                recheck_hours=recheck_hours,
            )
        return FreshnessAssessment(
            stale=False,
            unknown_age=True,
            reasons=("no_version_timestamps",),
            matched_category=matched_category,
            age_hours=None,
            recheck_hours=recheck_hours,
        )

    max_age = max(ages)
    limit = recheck_hours if recheck_hours is not None else 336.0
    stale = max_age > limit
    if stale:
        reasons.append(f"evidence_age_hours={round(max_age, 2)}>recheck_hours={limit}")
    return FreshnessAssessment(
        stale=stale,
        unknown_age=False,
        reasons=tuple(reasons),
        matched_category=matched_category,
        age_hours=round(max_age, 2),
        recheck_hours=limit,
    )


def decide_retrieval(
    *,
    question: str,
    jurisdiction: str,
    pathway_id: str | None,
    external_mode: ExternalMode | str,
    substantive_internal_count: int,
    internal_status: str,
    freshness: FreshnessAssessment | None = None,
    has_numeric_conflict: bool = False,
    connectors_available: bool = True,
) -> RetrievalPolicyDecision:
    mode = (
        external_mode
        if isinstance(external_mode, ExternalMode)
        else ExternalMode(str(external_mode).strip().lower())
    )
    if mode == ExternalMode.OFF:
        return RetrievalPolicyDecision(
            decision=RetrievalDecision.INTERNAL_ONLY,
            reasons=("external_mode=off",),
            run_external=False,
        )
    if mode == ExternalMode.ON:
        return RetrievalPolicyDecision(
            decision=RetrievalDecision.INTERNAL_PLUS_EXTERNAL,
            reasons=("external_mode=on",),
            run_external=True,
        )

    # AUTO
    reasons: list[str] = []
    high_risk = detect_high_risk_categories(question)
    queue_reasons = review_queue_uncertainty(
        jurisdiction=jurisdiction, pathway_id=pathway_id
    )
    thin = substantive_internal_count < 2 or internal_status in {
        "no_results",
        "ok_empty",
        "error",
        "unsupported_filter",
    }
    stale = bool(freshness and freshness.stale)
    salary_or_fee = any(c in high_risk for c in ("salary_threshold", "application_fee"))
    change_risk = "regulatory_change" in high_risk
    historical = bool(_HISTORICAL_RE.search(question or "")) and not change_risk

    if thin:
        reasons.append("insufficient_internal_evidence")
    if stale:
        reasons.extend(freshness.reasons if freshness else ("stale_internal_evidence",))
    if high_risk:
        reasons.append("high_risk:" + ",".join(high_risk))
    if queue_reasons:
        reasons.extend(queue_reasons)
    if has_numeric_conflict:
        reasons.append("internal_external_or_prior_numeric_conflict")
    if change_risk:
        reasons.append("question_implies_current_or_changed_rules")
    if historical:
        reasons.append("historical_question_prefer_internal_evidence")

    # Live connectors typically return current pages; prefer curated historical
    # evidence when the question is clearly about a past rule.
    needs_external = bool(
        thin
        or (stale and not historical)
        or (salary_or_fee and not historical)
        or change_risk
        or (queue_reasons and not historical)
        or has_numeric_conflict
    )

    if not needs_external:
        return RetrievalPolicyDecision(
            decision=RetrievalDecision.INTERNAL_ONLY,
            reasons=("sufficient_fresh_low_risk_internal",),
            high_risk_categories=tuple(high_risk),
            freshness=freshness,
            run_external=False,
        )

    if not connectors_available:
        # Still mark verification required for high-risk current figures.
        if salary_or_fee or stale or queue_reasons:
            return RetrievalPolicyDecision(
                decision=RetrievalDecision.EXTERNAL_VERIFICATION_REQUIRED,
                reasons=tuple(reasons + ["no_mapped_external_connectors"]),
                high_risk_categories=tuple(high_risk),
                freshness=freshness,
                run_external=False,
                require_verification=True,
            )
        return RetrievalPolicyDecision(
            decision=RetrievalDecision.INTERNAL_ONLY,
            reasons=tuple(reasons + ["no_mapped_external_connectors"]),
            high_risk_categories=tuple(high_risk),
            freshness=freshness,
            run_external=False,
        )

    if salary_or_fee and (stale or thin or queue_reasons or change_risk):
        return RetrievalPolicyDecision(
            decision=RetrievalDecision.EXTERNAL_VERIFICATION_REQUIRED,
            reasons=tuple(reasons),
            high_risk_categories=tuple(high_risk),
            freshness=freshness,
            run_external=True,
            require_verification=True,
        )

    return RetrievalPolicyDecision(
        decision=RetrievalDecision.INTERNAL_PLUS_EXTERNAL,
        reasons=tuple(reasons),
        high_risk_categories=tuple(high_risk),
        freshness=freshness,
        run_external=True,
        require_verification=False,
    )


def reconcile_effective_dates(
    evidence: list[EvidenceItem],
    *,
    now: datetime | None = None,
) -> ReconciliationNotes:
    """Conservative scan for future-effective language; never invent dates."""
    notes = ReconciliationNotes()
    now = now or datetime.now(timezone.utc)
    year_now = now.year
    for item in evidence:
        text = item.passage_text or ""
        for match in _FUTURE_EFFECTIVE_RE.finditer(text):
            snippet = match.group(0)
            years = [int(y) for y in _YEAR_RE.findall(snippet)]
            if years and max(years) > year_now:
                notes.future_effective_mentions.append(snippet[:120])
                notes.warnings.append(
                    "future_effective_language_detected:"
                    "do_not_treat_as_currently_applicable"
                )
                notes.missing_information.append(
                    "effective_date_confirmation_for_cited_requirement"
                )
    # Deduplicate warnings
    notes.warnings = list(dict.fromkeys(notes.warnings))
    notes.missing_information = list(dict.fromkeys(notes.missing_information))
    return notes


def resolve_external_mode(
    *,
    external_mode: str | None,
    include_external: bool | None,
) -> ExternalMode:
    """Resolve legacy include_external boolean into external_mode."""
    if include_external is True:
        return ExternalMode.ON
    if include_external is False:
        return ExternalMode.OFF
    raw = (external_mode or "auto").strip().lower()
    if raw in {"auto", "off", "on"}:
        return ExternalMode(raw)
    return ExternalMode.AUTO
