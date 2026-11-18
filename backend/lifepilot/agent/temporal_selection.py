"""Deterministic temporal evidence selection and outcome signaling (Phase 10B.3).

Never treats crawl dates or DocumentVersion.is_current as legal applicability.
Never substitutes current rules when historical evidence is unavailable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from lifepilot.agent.evidence import (
    EvidenceItem,
    EvidenceSourceType,
    EvidenceVerificationState,
)


class TemporalQueryMode(str, Enum):
    CURRENT = "current"
    EXPLICIT_CURRENT = "explicit_current"
    HISTORICAL = "historical"
    FUTURE = "future"


class TemporalOutcomeStatus(str, Enum):
    REVIEWED_RULE_FOUND = "reviewed_rule_found"
    HISTORICAL_CANDIDATE_UNVERIFIED = "historical_candidate_unverified"
    CURRENT_CANDIDATE_UNVERIFIED = "current_candidate_unverified"
    INSUFFICIENT_FOR_DATE = "insufficient_for_date"
    CONFLICTING_TEMPORAL_EVIDENCE = "conflicting_temporal_evidence"
    CURRENT_AVAILABLE_HISTORICAL_UNAVAILABLE = (
        "current_available_historical_unavailable"
    )
    EXCLUDED_NON_APPLICABLE_STATUS = "excluded_non_applicable_status"


_NON_CURRENT_LEGAL_STATUSES = frozenset(
    {
        "archived",
        "withdrawn",
        "superseded",
        "historical",
        "proposed",
        "proposed_not_effective",
        "proposed_or_preparatory",
    }
)

_HISTORICAL_TEXT_RE = re.compile(
    r"(?i)\b("
    r"previously|former(?:ly)?|prior to|before\s+\d{1,2}|before\s+20\d{2}|"
    r"until\s+20\d{2}|until\s+\d{1,2}|historical|superseded|replaced by|"
    r"no longer applies|used to|was\s+(?:£|\$|€|gbp|eur|usd|set\s+at|fixed\s+at)|"
    r"applied\s+in\s+(?:19|20)\d{2}|in\s+(?:19|20)\d{2}\b|"
    r"from\s+(?:19|20)\d{2}\s+(?:to|until|through)\s+(?:19|20)\d{2}"
    r")\b"
)
_ISO_DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def resolve_temporal_context(
    as_of_date: date | None,
    *,
    today_utc: date | None = None,
) -> tuple[date, TemporalQueryMode]:
    """Resolve reference date and explicit query mode at request execution time."""
    today = today_utc or datetime.now(timezone.utc).date()
    if as_of_date is None:
        return today, TemporalQueryMode.CURRENT
    if as_of_date < today:
        return as_of_date, TemporalQueryMode.HISTORICAL
    if as_of_date == today:
        return as_of_date, TemporalQueryMode.EXPLICIT_CURRENT
    return as_of_date, TemporalQueryMode.FUTURE


def _parse_iso_date(raw: str | None) -> date | None:
    if not raw or not isinstance(raw, str):
        return None
    m = _ISO_DATE_PREFIX_RE.match(raw.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _passage_explicitly_addresses_historical_date(
    item: EvidenceItem,
    as_of_date: date,
) -> bool:
    """True only when a current webpage text explicitly anchors to the historical period."""
    text = item.passage_text or ""
    year_str = str(as_of_date.year)
    if year_str not in text:
        return False
    return bool(_HISTORICAL_TEXT_RE.search(text))


@dataclass
class TemporalSelectionResult:
    kept_evidence: list[EvidenceItem] = field(default_factory=list)
    excluded_items: list[dict[str, Any]] = field(default_factory=list)
    temporal_outcome: TemporalOutcomeStatus = (
        TemporalOutcomeStatus.CURRENT_CANDIDATE_UNVERIFIED
    )
    warnings: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    current_available_for_historical: bool = False
    has_reviewed_rule: bool = False


def select_temporal_evidence(
    evidence: list[EvidenceItem],
    *,
    resolved_reference_date: date | None,
    temporal_mode: TemporalQueryMode | str,
    period_start: date | None = None,
    period_end: date | None = None,
    has_rule_conflicts: bool = False,
    has_numeric_conflict: bool = False,
    cohort_clarification_required: bool = False,
    period_clarification_required: bool = False,
    unreviewed_rules_excluded_count: int = 0,
    unknown_date_rules_excluded_count: int = 0,
) -> TemporalSelectionResult:
    """Deterministically filter & annotate evidence for ``resolved_reference_date`` or period."""
    mode = (
        temporal_mode
        if isinstance(temporal_mode, TemporalQueryMode)
        else TemporalQueryMode(str(temporal_mode))
    )
    eff_ref_date = resolved_reference_date or period_end or datetime.now(timezone.utc).date()
    eff_start_date = resolved_reference_date or period_start or eff_ref_date
    kept: list[EvidenceItem] = []
    excluded: list[dict[str, Any]] = []
    warnings: list[str] = []
    missing: list[str] = []
    saw_current_only_for_historical = False

    # Identify documents that have an explicit historical (non-current) version
    # in the candidate pool so current versions do not crowd out historical ones.
    docs_with_historical_version: set[Any] = {
        item.document_id
        for item in evidence
        if item.source_type == EvidenceSourceType.INTERNAL_RAG
        and item.document_id is not None
        and (
            not bool((item.temporal_provenance or {}).get("is_current_version", True))
            or (item.legal_status or "").lower()
            in {"historical", "superseded", "archived", "transitional"}
        )
    }

    for item in evidence:
        if item.source_type == EvidenceSourceType.CURATED_RULE:
            kept.append(item)
            continue

        status = (item.legal_status or "unknown").strip().lower()
        tprov = item.temporal_provenance or {}
        is_current_ver = bool(tprov.get("is_current_version", True))
        eff_date = _parse_iso_date(item.explicit_effective_at)
        pub_date = _parse_iso_date(item.source_published_at)

        if mode in {TemporalQueryMode.CURRENT, TemporalQueryMode.EXPLICIT_CURRENT}:
            if status in _NON_CURRENT_LEGAL_STATUSES:
                excluded.append(
                    {
                        "evidence_id": item.evidence_id,
                        "source_url": item.source_url,
                        "reason": f"excluded_status_for_current:{status}",
                    }
                )
                continue
            if status == "future_pending" or (
                eff_date is not None and eff_date > eff_ref_date
            ):
                excluded.append(
                    {
                        "evidence_id": item.evidence_id,
                        "source_url": item.source_url,
                        "reason": "excluded_future_effective_not_current",
                    }
                )
                continue
            kept.append(item)
            continue

        if mode == TemporalQueryMode.HISTORICAL:
            if item.source_type == EvidenceSourceType.EXTERNAL_API:
                if item.provider != "us_federal_register":
                    excluded.append(
                        {
                            "evidence_id": item.evidence_id,
                            "source_url": item.source_url,
                            "reason": "excluded_live_current_connector_for_historical",
                        }
                    )
                    continue
                if status in {"proposed", "proposed_not_effective", "proposed_or_preparatory"}:
                    excluded.append(
                        {
                            "evidence_id": item.evidence_id,
                            "source_url": item.source_url,
                            "reason": f"excluded_proposed_for_historical:{status}",
                        }
                    )
                    continue
                if pub_date is not None and pub_date > eff_ref_date:
                    excluded.append(
                        {
                            "evidence_id": item.evidence_id,
                            "source_url": item.source_url,
                            "reason": "excluded_published_after_as_of_date",
                        }
                    )
                    continue
                if eff_date is not None and eff_date > eff_ref_date:
                    excluded.append(
                        {
                            "evidence_id": item.evidence_id,
                            "source_url": item.source_url,
                            "reason": "excluded_effective_after_as_of_date",
                        }
                    )
                    continue
                kept.append(item)
                continue

            # INTERNAL_RAG under HISTORICAL mode:
            if status in {"proposed", "proposed_not_effective", "proposed_or_preparatory", "future_pending"}:
                if eff_date is None or eff_date > eff_ref_date:
                    excluded.append(
                        {
                            "evidence_id": item.evidence_id,
                            "source_url": item.source_url,
                            "reason": f"excluded_non_historical_status:{status}",
                        }
                    )
                    continue

            is_historical_snapshot = (not is_current_ver) or (
                status in {"historical", "superseded", "archived", "transitional"}
            )
            if is_historical_snapshot:
                if (
                    pub_date is not None
                    and pub_date > eff_ref_date
                    and not _passage_explicitly_addresses_historical_date(
                        item, eff_start_date
                    )
                ):
                    excluded.append(
                        {
                            "evidence_id": item.evidence_id,
                            "source_url": item.source_url,
                            "reason": "excluded_snapshot_published_after_as_of_date",
                        }
                    )
                    continue
                kept.append(item)
                continue

            # Current-law webpage (is_current_version=True, legal_status in unknown/in_force):
            if (
                item.document_id in docs_with_historical_version
                and not _passage_explicitly_addresses_historical_date(
                    item, eff_start_date
                )
            ):
                saw_current_only_for_historical = True
                excluded.append(
                    {
                        "evidence_id": item.evidence_id,
                        "source_url": item.source_url,
                        "reason": "excluded_current_version_superseded_by_historical_snapshot",
                    }
                )
                continue

            if _passage_explicitly_addresses_historical_date(
                item, eff_start_date
            ):
                kept.append(item)
                warnings.append(
                    "historical_candidate_from_current_document_text_unverified"
                )
                continue

            saw_current_only_for_historical = True
            excluded.append(
                {
                    "evidence_id": item.evidence_id,
                    "source_url": item.source_url,
                    "reason": "excluded_current_page_not_historical_proof",
                }
            )
            continue

        # FUTURE mode (as_of_date > today_utc):
        if status in _NON_CURRENT_LEGAL_STATUSES:
            excluded.append(
                {
                    "evidence_id": item.evidence_id,
                    "source_url": item.source_url,
                    "reason": f"excluded_status_for_future:{status}",
                }
            )
            continue
        if eff_date is not None and eff_date > eff_ref_date:
            excluded.append(
                {
                    "evidence_id": item.evidence_id,
                    "source_url": item.source_url,
                    "reason": "excluded_effective_after_requested_future_date",
                }
            )
            continue
        kept.append(item)

    if excluded:
        reasons_preview = sorted({e["reason"] for e in excluded})[:4]
        warnings.append(
            f"temporal_excluded_non_applicable_sources:{len(excluded)}:"
            + ",".join(reasons_preview)
        )

    has_reviewed_rule = any(
        e.source_type == EvidenceSourceType.CURATED_RULE
        and e.verification_state == EvidenceVerificationState.HUMAN_VERIFIED_RULE
        for e in kept
    )
    evidence_conflicts = any(
        bool((e.temporal_provenance or {}).get("has_conflicts")) for e in kept
    )

    date_label = (
        resolved_reference_date.isoformat()
        if resolved_reference_date is not None
        else f"{eff_start_date.year}"
    )

    if has_rule_conflicts or has_numeric_conflict or evidence_conflicts:
        outcome = TemporalOutcomeStatus.CONFLICTING_TEMPORAL_EVIDENCE
        warnings.append("temporal_outcome:conflicting_temporal_evidence")
    elif has_reviewed_rule:
        outcome = TemporalOutcomeStatus.REVIEWED_RULE_FOUND
    elif kept:
        if mode == TemporalQueryMode.HISTORICAL:
            outcome = TemporalOutcomeStatus.HISTORICAL_CANDIDATE_UNVERIFIED
            warnings.append(
                "temporal_outcome:historical_candidate_unverified:"
                "applicability_for_requested_date_not_human_verified"
            )
        else:
            outcome = TemporalOutcomeStatus.CURRENT_CANDIDATE_UNVERIFIED
            if mode == TemporalQueryMode.FUTURE:
                warnings.append(
                    "future_date_applicability_unconfirmed_without_reviewed_rule"
                )
    else:
        if mode == TemporalQueryMode.HISTORICAL and saw_current_only_for_historical:
            outcome = TemporalOutcomeStatus.CURRENT_AVAILABLE_HISTORICAL_UNAVAILABLE
            warnings.append(
                "temporal_outcome:current_available_historical_unavailable:"
                "current_rules_not_substituted_for_requested_date"
            )
            missing.append(f"historical_evidence_for_date:{date_label}")
        elif excluded:
            outcome = TemporalOutcomeStatus.EXCLUDED_NON_APPLICABLE_STATUS
            warnings.append("temporal_outcome:excluded_non_applicable_status")
            missing.append(f"applicable_evidence_for_date:{date_label}")
        else:
            outcome = TemporalOutcomeStatus.INSUFFICIENT_FOR_DATE
            warnings.append("temporal_outcome:insufficient_for_date")
            missing.append(f"evidence_for_date:{date_label}")

    if cohort_clarification_required:
        missing.append("applicant_cohort_clarification")
    if period_clarification_required:
        missing.append(f"exact_as_of_date_within_period:{date_label}")
    if unreviewed_rules_excluded_count and not has_reviewed_rule:
        warnings.append(
            f"unreviewed_rules_present_not_treated_as_authoritative:{unreviewed_rules_excluded_count}"
        )
    if unknown_date_rules_excluded_count and not has_reviewed_rule:
        warnings.append(
            f"unknown_effective_date_rules_not_treated_as_universal:{unknown_date_rules_excluded_count}"
        )
    if any(
        getattr(e, "source_authority_category", None) == "government_statistical_dataset"
        for e in kept
    ):
        warnings.append("statistical_dataset_present_not_normative_authority")
    if any(
        getattr(e, "source_authority_category", None) == "government_portal"
        for e in kept
    ):
        warnings.append("portal_page_present_not_normative_authority")
    if any(bool((e.provenance or {}).get("eu_level_source")) for e in kept):
        warnings.append("eu_level_source_requires_national_implementation")

    return TemporalSelectionResult(
        kept_evidence=kept,
        excluded_items=excluded,
        temporal_outcome=outcome,
        warnings=list(dict.fromkeys(warnings)),
        missing_information=list(dict.fromkeys(missing)),
        current_available_for_historical=saw_current_only_for_historical,
        has_reviewed_rule=has_reviewed_rule,
    )
