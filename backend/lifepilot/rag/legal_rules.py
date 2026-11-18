"""Deterministic date-aware lookup for curated LegalRule records (Phase 10B.3).

Only ``review_status='approved'`` and ``verification_status='human_verified'``
rules with explicit effective-date semantics are treated as verified applicable
rules. Unknown effective dates and unverified/unreviewed rules are never
promoted or auto-selected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import inspect, or_, select
from sqlalchemy.orm import Session

from lifepilot.db.models import LegalRule

_ACTIVE_DATE_STATUSES = frozenset(
    {
        "known_bounded",
        "known_open_ended",
        "announced_future",
        "superseded",
        "expired",
    }
)


@dataclass
class LegalRuleLookupResult:
    status: str  # ok | empty | schema_unavailable
    as_of_date: date | None
    jurisdiction: str
    pathway_id: str | None = None
    matched_rules: list[LegalRule] = field(default_factory=list)
    cohort_alternatives: list[LegalRule] = field(default_factory=list)
    unreviewed_excluded_count: int = 0
    unknown_date_excluded_count: int = 0
    cohort_clarification_required: bool = False
    period_clarification_required: bool = False
    multiple_periods_in_window: bool = False
    conflicting_rule_keys: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def legal_rules_table_available(session: Session) -> bool:
    """Return True if ``legal_rules`` exists on the bound database."""
    try:
        bind = session.get_bind()
        return bool(inspect(bind).has_table("legal_rules"))
    except Exception:  # noqa: BLE001
        return False


def rule_applies_on_date(rule: LegalRule, as_of_date: date) -> tuple[bool, str]:
    """Evaluate deterministic effective-period boundaries on ``as_of_date``.

    Rules with ``effective_date_status`` in (``unknown``, ``partially_known``)
    are never treated as universally applicable.
    """
    status = (rule.effective_date_status or "unknown").strip().lower()
    if status == "unknown":
        return False, "unknown_effective_dates"
    if status == "partially_known":
        return False, "partially_known_effective_dates"
    if status not in _ACTIVE_DATE_STATUSES:
        return False, f"unsupported_effective_status:{status}"

    if rule.effective_from is None:
        return False, "missing_effective_from"

    if rule.effective_from_inclusive:
        if as_of_date < rule.effective_from:
            return False, "before_effective_from"
    else:
        if as_of_date <= rule.effective_from:
            return False, "on_or_before_exclusive_effective_from"

    if rule.effective_to is not None:
        if rule.effective_to_inclusive:
            if as_of_date > rule.effective_to:
                return False, "after_effective_to"
        else:
            if as_of_date >= rule.effective_to:
                return False, "on_or_after_exclusive_effective_to"
        return True, "within_bounded_period"

    # Open-ended upper bound is valid ONLY for known_open_ended / announced_future.
    if status in {"known_open_ended", "announced_future"}:
        return True, "within_open_ended_period"
    return False, f"open_upper_bound_incompatible_with_{status}"


def _parse_iso_date(val: Any) -> date | None:
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str) and val.strip():
        try:
            return date.fromisoformat(val.strip()[:10])
        except ValueError:
            return None
    return None


def _is_general_cohort(rule: LegalRule) -> bool:
    return (
        (rule.cohort_key or "general") == "general"
        and (rule.cohort_category or "general") == "general"
        and not bool(rule.is_transitional)
    )


def cohort_matches_circumstances(
    rule: LegalRule,
    user_circumstances: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Match applicant cohort without guessing when cohort is unknown.

    - General non-transitional rules match unless the caller explicitly specifies
      a different ``cohort_key``.
    - Cohort-specific / transitional rules match ONLY when ``user_circumstances``
      explicitly confirms the ``cohort_key`` or satisfies all structured
      ``cohort_conditions``.
    """
    circum = user_circumstances or {}
    requested_cohort = (
        str(circum.get("cohort_key") or "").strip() or None
        if isinstance(circum, dict)
        else None
    )

    if _is_general_cohort(rule):
        if requested_cohort and requested_cohort != "general":
            return False, "caller_requested_specific_cohort"
        return True, "general_cohort"

    if requested_cohort and requested_cohort == rule.cohort_key:
        return True, "explicit_cohort_key_match"

    conditions = rule.cohort_conditions if isinstance(rule.cohort_conditions, dict) else {}
    if not conditions or not circum:
        return False, "cohort_unconfirmed"

    for key, expected in conditions.items():
        if key == "prior_permission_before":
            cutoff = _parse_iso_date(expected)
            actual = _parse_iso_date(
                circum.get("prior_permission_date")
                or circum.get("first_cos_date")
                or circum.get("first_permission_date")
            )
            if cutoff is None or actual is None or not (actual < cutoff):
                return False, f"condition_not_met:{key}"
        elif key == "age_under":
            age = circum.get("age")
            if not isinstance(age, (int, float)) or age >= float(expected):
                return False, f"condition_not_met:{key}"
        elif key == "age_at_least":
            age = circum.get("age")
            if not isinstance(age, (int, float)) or age < float(expected):
                return False, f"condition_not_met:{key}"
        else:
            if key not in circum or circum.get(key) != expected:
                return False, f"condition_not_met:{key}"

    return True, "structured_cohort_conditions_met"


def _detect_rule_conflicts(rules: list[LegalRule]) -> list[str]:
    """Flag multiple matched verified rules with the same identity/type & cohort differing in value."""
    by_slot: dict[tuple[str | None, str, str], set[tuple[str, str | None, str | None]]] = {}
    for r in rules:
        slot = (r.pathway_id, r.requirement_type, r.cohort_key)
        sig = (
            str(r.numeric_value) if r.numeric_value is not None else "",
            r.currency_code,
            r.amount_unit,
        )
        by_slot.setdefault(slot, set()).add(sig)
    conflicts: list[str] = []
    for slot, sigs in by_slot.items():
        if len(sigs) > 1:
            conflicts.append(
                f"conflicting_verified_rules:{slot[0] or 'jurisdiction'}:{slot[1]}:{slot[2]}"
            )
    return conflicts


def rule_overlaps_period(
    rule: LegalRule,
    period_start: date,
    period_end: date,
) -> tuple[bool, str]:
    """True when a reviewed rule's effective window overlaps [period_start, period_end]."""
    status = (rule.effective_date_status or "unknown").strip().lower()
    if status in {"unknown", "partially_known"}:
        return False, f"{status}_effective_dates"
    if status not in _ACTIVE_DATE_STATUSES or rule.effective_from is None:
        return False, "missing_or_unsupported_effective_from"
    if rule.effective_from_inclusive:
        if rule.effective_from > period_end:
            return False, "starts_after_period"
    else:
        if rule.effective_from >= period_end:
            return False, "starts_on_or_after_period_end"
    if rule.effective_to is not None:
        if rule.effective_to_inclusive:
            if rule.effective_to < period_start:
                return False, "ended_before_period"
        else:
            if rule.effective_to <= period_start:
                return False, "ended_on_or_before_period"
        return True, "overlaps_bounded_period"
    if status in {"known_open_ended", "announced_future"}:
        return True, "overlaps_open_ended_period"
    return False, f"open_upper_bound_incompatible_with_{status}"


def query_reviewed_legal_rules(
    session: Session,
    *,
    jurisdiction: str,
    as_of_date: date | None,
    pathway_id: str | None = None,
    requirement_category: str | None = None,
    rule_key: str | None = None,
    user_circumstances: dict[str, Any] | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> LegalRuleLookupResult:
    """Deterministically retrieve human-verified, date-applicable LegalRule rows."""
    jur = (jurisdiction or "").strip().upper()
    out = LegalRuleLookupResult(
        status="empty",
        as_of_date=as_of_date,
        jurisdiction=jur,
        pathway_id=(pathway_id or "").strip() or None,
    )
    if not jur:
        return out
    if as_of_date is None and (period_start is None or period_end is None):
        out.warnings.append("no_reference_date_or_period_supplied_for_rule_lookup")
        return out
    if not legal_rules_table_available(session):
        out.status = "schema_unavailable"
        out.warnings.append("legal_rules_table_unavailable_on_connected_schema")
        return out

    stmt = select(LegalRule).where(LegalRule.jurisdiction_code == jur)
    if out.pathway_id:
        stmt = stmt.where(
            or_(
                LegalRule.pathway_id == out.pathway_id,
                LegalRule.pathway_id.is_(None),
            )
        )
    if requirement_category:
        stmt = stmt.where(LegalRule.requirement_category == requirement_category)
    if rule_key:
        stmt = stmt.where(LegalRule.rule_key == rule_key)

    stmt = stmt.order_by(
        LegalRule.pathway_id.asc().nulls_last(),
        LegalRule.rule_key.asc(),
        LegalRule.rule_revision.desc(),
    )
    candidates = list(session.scalars(stmt).all())

    date_matching_verified: list[LegalRule] = []
    for rule in candidates:
        if as_of_date is not None:
            date_ok, date_reason = rule_applies_on_date(rule, as_of_date)
        else:
            assert period_start is not None and period_end is not None
            date_ok, date_reason = rule_overlaps_period(rule, period_start, period_end)

        is_verified = (
            rule.review_status == "approved"
            and rule.verification_status == "human_verified"
        )
        if not is_verified:
            if date_ok or date_reason in {"unknown_effective_dates", "partially_known_effective_dates"}:
                out.unreviewed_excluded_count += 1
            continue
        if not date_ok:
            if date_reason in {"unknown_effective_dates", "partially_known_effective_dates"}:
                out.unknown_date_excluded_count += 1
            continue
        date_matching_verified.append(rule)

    for rule in date_matching_verified:
        matched_cohort, _reason = cohort_matches_circumstances(rule, user_circumstances)
        if matched_cohort:
            out.matched_rules.append(rule)
        elif not _is_general_cohort(rule):
            out.cohort_alternatives.append(rule)

    if out.unreviewed_excluded_count:
        out.warnings.append(
            f"unreviewed_legal_rules_excluded:{out.unreviewed_excluded_count}"
        )
    if out.unknown_date_excluded_count:
        out.warnings.append(
            f"unknown_effective_date_rules_excluded:{out.unknown_date_excluded_count}"
        )
    if out.cohort_alternatives and not out.matched_rules:
        out.cohort_clarification_required = True
        out.warnings.append(
            "cohort_specific_rules_exist_but_applicant_cohort_unknown"
        )
    elif out.cohort_alternatives:
        out.warnings.append(
            f"additional_cohort_specific_rules_present:{len(out.cohort_alternatives)}"
        )

    if as_of_date is None and period_start is not None and period_end is not None:
        distinct_windows = {
            (r.effective_from, r.effective_to) for r in out.matched_rules
        }
        if len(distinct_windows) > 1:
            out.multiple_periods_in_window = True
            out.period_clarification_required = True
            out.warnings.append(
                f"multiple_legal_periods_in_requested_window:"
                f"{period_start.isoformat()}..{period_end.isoformat()}"
            )
    else:
        out.conflicting_rule_keys = _detect_rule_conflicts(out.matched_rules)
        if out.conflicting_rule_keys:
            out.warnings.extend(out.conflicting_rule_keys)

    if out.matched_rules:
        out.status = "ok"
    return out
