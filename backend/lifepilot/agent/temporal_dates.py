"""Deterministic temporal expression parsing and question date-intent detection (Phase 10B.4).

Parses unambiguous ISO and named-month dates and evaluates effective/expiry
boundaries against a resolved reference date. Rejects ambiguous numeric dates
(e.g., 04/05/2026) and never invents exact dates for year-only queries.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable

from lifepilot.agent.temporal_selection import TemporalQueryMode

_MONTHS: dict[str, int] = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

_MONTH_NAMES_RE = (
    r"January|February|March|April|May|June|July|August|"
    r"September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)

_EXACT_DATE_RE = re.compile(
    rf"(?P<iso>\b(?:19|20)\d{{2}}-\d{{2}}-\d{{2}}\b)|"
    rf"(?P<dmy>\b\d{{1,2}}\s+(?:{_MONTH_NAMES_RE})\s+(?:19|20)\d{{2}}\b)|"
    rf"(?P<mdy>\b(?:{_MONTH_NAMES_RE})\s+\d{{1,2}},?\s+(?:19|20)\d{{2}}\b)",
    re.IGNORECASE,
)

_AMBIGUOUS_NUMERIC_DATE_RE = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}[/-](?:19|20)\d{2}|(?:19|20)\d{2}/\d{2}/\d{2})\b"
)

_MONTH_YEAR_RE = re.compile(
    rf"\b(?:in|during|from|before|until|through|effective|after|around|as of)\s+"
    rf"(?P<month>{_MONTH_NAMES_RE})\s+(?P<year>(?:19|20)\d{{2}})\b",
    re.IGNORECASE,
)

_YEAR_ONLY_CUE_RE = re.compile(
    r"\b(?P<cue>in|during|around|for|before|until|through|effective(?:\s+from)?|"
    r"from|after|since|starting)\s+(?P<year>(?:19|20)\d{2})\b",
    re.IGNORECASE,
)

_START_CUES_RE = re.compile(
    r"(?i)\b("
    r"effective\s+(?:from|on)|comes?\s+into\s+(?:force|effect)(?:\s+on)?|"
    r"with\s+effect\s+from|applies?\s+from|starting(?:\s+(?:from|on))?|"
    r"on\s+or\s+after|from|after|between"
    r")\s*$"
)
_END_CUES_RE = re.compile(
    r"(?i)\b("
    r"no\s+longer\s+applies(?:\s+(?:from|after|on))?|"
    r"ceases?\s+to\s+apply(?:\s+(?:from|after|on))?|"
    r"expires?(?:\s+on)?|ends?(?:\s+on)?|"
    r"before|prior\s+to|until|through|up\s+to"
    r")\s*$"
)
_RANGE_END_CONTEXT_RE = re.compile(
    rf"(?i)\b(?:from|between)\s+(?:(?:19|20)\d{{2}}-\d{{2}}-\d{{2}}|"
    rf"\d{{1,2}}\s+(?:{_MONTH_NAMES_RE})\s+(?:19|20)\d{{2}}|"
    rf"(?:{_MONTH_NAMES_RE})\s+\d{{1,2}},?\s+(?:19|20)\d{{2}})\s+(?:to|and)\s*$"
)

# Cohort context cues: dates following these describe applicant history, not query as_of_date.
_COHORT_DATE_PREFIX_RE = re.compile(
    r"(?i)\b("
    r"cos|certificate\s+of\s+sponsorship|visa|permit|leave|permission|"
    r"issued|granted|assigned|obtained|held|applied|started|entered|arrival"
    r")(?:\s+\w+){0,4}\s+(?:before|prior\s+to|on\s+or\s+before|since|after|from)\s*$"
)

# Query-level historical/future reference cues for an exact date in a question.
_QUERY_EXACT_DATE_PREFIX_RE = re.compile(
    r"(?i)\b("
    r"as\s+of|as\s+at|applicable\s+on|in\s+force\s+on|effective\s+on|"
    r"applied\s+on|valid\s+on|rules?\s+on|threshold\s+on|fee\s+on|on"
    r")\s*$"
)
_QUESTION_HISTORICAL_VERB_RE = re.compile(
    r"(?i)\b(what\s+was|what\s+were|did|used\s+to|previously|formerly|historical|applied)\b"
)


@dataclass(frozen=True)
class EffectiveDateMention:
    raw_snippet: str
    date_literal: str
    role: str  # start | end | unspecified
    parsed_date: date | None
    inclusive: bool | None
    granularity: str  # day | month | year | ambiguous
    year: int | None = None
    month: int | None = None
    is_future_for_reference: bool = False
    is_expired_for_reference: bool = False
    boundary_unresolved: bool = False
    ambiguous: bool = False


@dataclass(frozen=True)
class QuestionTemporalIntent:
    temporal_mode: TemporalQueryMode
    resolved_reference_date: date | None
    inferred_from_question: bool = False
    explicit_override: bool = False
    period_year: int | None = None
    period_month: int | None = None
    period_start: date | None = None
    period_end: date | None = None
    ambiguous_date_literal: str | None = None
    clarification_required: bool = False
    warnings: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()


def parse_unambiguous_date_literal(raw: str | None) -> date | None:
    """Parse ``YYYY-MM-DD``, ``DD Month YYYY``, or ``Month DD, YYYY`` strictly."""
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return None
    if re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    m_dmy = re.fullmatch(
        rf"(\d{{1,2}})\s+({_MONTH_NAMES_RE})\s+((?:19|20)\d{{2}})",
        text,
        re.IGNORECASE,
    )
    if m_dmy:
        month = _MONTHS.get(m_dmy.group(2).lower())
        if month is None:
            return None
        try:
            return date(int(m_dmy.group(3)), month, int(m_dmy.group(1)))
        except ValueError:
            return None
    m_mdy = re.fullmatch(
        rf"({_MONTH_NAMES_RE})\s+(\d{{1,2}}),?\s+((?:19|20)\d{{2}})",
        text,
        re.IGNORECASE,
    )
    if m_mdy:
        month = _MONTHS.get(m_mdy.group(1).lower())
        if month is None:
            return None
        try:
            return date(int(m_mdy.group(3)), month, int(m_mdy.group(2)))
        except ValueError:
            return None
    return None


def _classify_preceding_cue(prefix: str) -> tuple[str, bool | None]:
    tail = (prefix or "")[-72:]
    if _RANGE_END_CONTEXT_RE.search(tail):
        return "end", True
    m_end = _END_CUES_RE.search(tail[-48:])
    if m_end:
        cue = m_end.group(1).lower()
        inclusive = False if ("before" in cue or "prior to" in cue) else True
        return "end", inclusive
    m_start = _START_CUES_RE.search(tail[-48:])
    if m_start:
        cue = m_start.group(1).lower()
        inclusive = False if cue == "after" else True
        return "start", inclusive
    return "unspecified", None


def extract_effective_date_mentions(
    text: str,
    *,
    reference_date: date | None = None,
) -> list[EffectiveDateMention]:
    """Extract temporal boundary mentions and evaluate them against ``reference_date``."""
    blob = text or ""
    if not blob.strip():
        return []
    ref = reference_date or datetime.now(timezone.utc).date()
    mentions: list[EffectiveDateMention] = []
    exact_spans: list[tuple[int, int]] = []

    for m in _EXACT_DATE_RE.finditer(blob):
        literal = m.group(0)
        parsed = parse_unambiguous_date_literal(literal)
        if parsed is None:
            continue
        exact_spans.append((m.start(), m.end()))
        prefix = blob[max(0, m.start() - 64) : m.start()]
        if _COHORT_DATE_PREFIX_RE.search(prefix):
            # Applicant-history cohort date (e.g. "CoS ... before 4 April 2024"),
            # not a rule effective/expiry boundary.
            continue
        role, inclusive = _classify_preceding_cue(prefix)
        # Skip plain un-cued date mentions inside non-temporal prose unless directional cue present.
        if role == "unspecified":
            continue
        snippet = blob[max(0, m.start() - 36) : m.end()].strip()

        is_future = False
        is_expired = False
        if role == "start":
            if parsed > ref or (parsed == ref and inclusive is False):
                is_future = True
        elif role == "end":
            if parsed < ref or (parsed == ref and inclusive is False):
                is_expired = True

        mentions.append(
            EffectiveDateMention(
                raw_snippet=snippet[:120],
                date_literal=literal,
                role=role,
                parsed_date=parsed,
                inclusive=inclusive,
                granularity="day",
                year=parsed.year,
                month=parsed.month,
                is_future_for_reference=is_future,
                is_expired_for_reference=is_expired,
                boundary_unresolved=False,
                ambiguous=False,
            )
        )

    def _overlaps_exact(start: int, end: int) -> bool:
        return any(not (end <= es or start >= ee) for es, ee in exact_spans)

    # Ambiguous numeric dates (DD/MM/YYYY etc.)
    for m in _AMBIGUOUS_NUMERIC_DATE_RE.finditer(blob):
        if _overlaps_exact(m.start(), m.end()):
            continue
        # YYYY/MM/DD is also rejected unless explicitly ISO hyphenated.
        mentions.append(
            EffectiveDateMention(
                raw_snippet=blob[max(0, m.start() - 24) : m.end()].strip()[:120],
                date_literal=m.group(0),
                role="unspecified",
                parsed_date=None,
                inclusive=None,
                granularity="ambiguous",
                boundary_unresolved=True,
                ambiguous=True,
            )
        )

    # Month + Year cues without day (e.g. "from April 2027")
    for m in _MONTH_YEAR_RE.finditer(blob):
        if _overlaps_exact(m.start(), m.end()):
            continue
        month = _MONTHS.get(m.group("month").lower())
        year = int(m.group("year"))
        if month is None:
            continue
        role, inclusive = _classify_preceding_cue(blob[max(0, m.start() - 48) : m.start() + 18])
        if role == "unspecified":
            # Check the preposition inside the match itself.
            head = m.group(0).split()[0].lower()
            if head in {"before"}:
                role, inclusive = "end", False
            elif head in {"until", "through"}:
                role, inclusive = "end", True
            elif head in {"from", "effective", "after"}:
                role, inclusive = "start", head != "after"
            else:
                continue

        is_future = False
        is_expired = False
        unresolved = False
        if role == "start":
            if (year, month) > (ref.year, ref.month):
                is_future = True
            elif (year, month) == (ref.year, ref.month):
                unresolved = True
        elif role == "end":
            if (year, month) < (ref.year, ref.month):
                is_expired = True
            elif (year, month) == (ref.year, ref.month):
                unresolved = True

        mentions.append(
            EffectiveDateMention(
                raw_snippet=m.group(0)[:120],
                date_literal=f"{m.group('month')} {year}",
                role=role,
                parsed_date=None,
                inclusive=inclusive,
                granularity="month",
                year=year,
                month=month,
                is_future_for_reference=is_future,
                is_expired_for_reference=is_expired,
                boundary_unresolved=unresolved,
                ambiguous=False,
            )
        )

    # Year-only cues without month/day (e.g. "effective from 2028", "until 2023")
    for m in _YEAR_ONLY_CUE_RE.finditer(blob):
        if _overlaps_exact(m.start(), m.end()):
            continue
        year = int(m.group("year"))
        cue = m.group("cue").lower()
        if cue in {"before"}:
            role, inclusive = "end", False
        elif cue in {"until", "through"}:
            role, inclusive = "end", True
        elif cue in {"from", "effective", "effective from", "after", "starting"}:
            role, inclusive = "start", cue != "after"
        else:
            continue

        is_future = False
        is_expired = False
        unresolved = False
        if role == "start":
            if year > ref.year:
                is_future = True
            elif year == ref.year:
                unresolved = True
        elif role == "end":
            if year < ref.year:
                is_expired = True
            elif year == ref.year:
                unresolved = True

        mentions.append(
            EffectiveDateMention(
                raw_snippet=m.group(0)[:120],
                date_literal=str(year),
                role=role,
                parsed_date=None,
                inclusive=inclusive,
                granularity="year",
                year=year,
                is_future_for_reference=is_future,
                is_expired_for_reference=is_expired,
                boundary_unresolved=unresolved,
                ambiguous=False,
            )
        )

    return mentions


def detect_question_temporal_intent(
    question: str,
    *,
    explicit_as_of_date: date | None,
    today_utc: date | None = None,
) -> QuestionTemporalIntent:
    """Detect unambiguous historical/future date intent from a question.

    Explicit ``as_of_date`` always takes precedence. Year-only historical
    questions return a period window (without inventing Jan 1 or Dec 31).
    """
    today = today_utc or datetime.now(timezone.utc).date()
    q = question or ""

    if explicit_as_of_date is not None:
        if explicit_as_of_date < today:
            mode = TemporalQueryMode.HISTORICAL
        elif explicit_as_of_date == today:
            mode = TemporalQueryMode.EXPLICIT_CURRENT
        else:
            mode = TemporalQueryMode.FUTURE
        return QuestionTemporalIntent(
            temporal_mode=mode,
            resolved_reference_date=explicit_as_of_date,
            explicit_override=True,
        )

    # 1. Ambiguous numeric date in query (e.g., 04/05/2022) — never guess day vs month.
    m_amb = _AMBIGUOUS_NUMERIC_DATE_RE.search(q)
    if m_amb:
        lit = m_amb.group(0)
        return QuestionTemporalIntent(
            temporal_mode=TemporalQueryMode.HISTORICAL,
            resolved_reference_date=None,
            ambiguous_date_literal=lit,
            clarification_required=True,
            warnings=(f"ambiguous_date_in_question:{lit}:use_YYYY_MM_DD",),
            missing_information=("unambiguous_as_of_date_YYYY_MM_DD",),
        )

    # 2. Unambiguous exact query reference date (excluding applicant cohort clauses).
    for m in _EXACT_DATE_RE.finditer(q):
        parsed = parse_unambiguous_date_literal(m.group(0))
        if parsed is None:
            continue
        prefix = q[max(0, m.start() - 60) : m.start()]
        if _COHORT_DATE_PREFIX_RE.search(prefix):
            continue
        if not (
            _QUERY_EXACT_DATE_PREFIX_RE.search(prefix)
            or (parsed < today and _QUESTION_HISTORICAL_VERB_RE.search(q))
        ):
            continue
        if parsed < today:
            return QuestionTemporalIntent(
                temporal_mode=TemporalQueryMode.HISTORICAL,
                resolved_reference_date=parsed,
                inferred_from_question=True,
                warnings=(f"temporal_reference_date_inferred_from_question:{parsed.isoformat()}",),
            )
        if parsed > today:
            return QuestionTemporalIntent(
                temporal_mode=TemporalQueryMode.FUTURE,
                resolved_reference_date=parsed,
                inferred_from_question=True,
                warnings=(f"temporal_reference_date_inferred_from_question:{parsed.isoformat()}",),
            )

    # 3. Month-year or year-only historical intent (period only; never invent Jan 1).
    m_my = _MONTH_YEAR_RE.search(q)
    if m_my:
        prefix = q[max(0, m_my.start() - 60) : m_my.start()]
        if not _COHORT_DATE_PREFIX_RE.search(prefix):
            month = _MONTHS.get(m_my.group("month").lower())
            year = int(m_my.group("year"))
            if month is not None and (year, month) < (today.year, today.month):
                last_day = calendar.monthrange(year, month)[1]
                return QuestionTemporalIntent(
                    temporal_mode=TemporalQueryMode.HISTORICAL,
                    resolved_reference_date=None,
                    inferred_from_question=True,
                    period_year=year,
                    period_month=month,
                    period_start=date(year, month, 1),
                    period_end=date(year, month, last_day),
                    warnings=(f"historical_period_detected:{year:04d}-{month:02d}",),
                    missing_information=(f"exact_as_of_date_within_period:{year:04d}-{month:02d}",),
                )

    for m_y in _YEAR_ONLY_CUE_RE.finditer(q):
        year = int(m_y.group("year"))
        cue = m_y.group("cue").lower()
        prefix = q[max(0, m_y.start() - 50) : m_y.start() + len(cue) + 1]
        if _COHORT_DATE_PREFIX_RE.search(prefix):
            continue
        if year < today.year and (
            cue in {"in", "during", "around"}
            or (cue == "before" and _QUESTION_HISTORICAL_VERB_RE.search(q))
        ):
            p_start = date(year, 1, 1) if cue in {"in", "during", "around"} else date(1900, 1, 1)
            p_end = date(year, 12, 31) if cue in {"in", "during", "around"} else date(year - 1, 12, 31)
            return QuestionTemporalIntent(
                temporal_mode=TemporalQueryMode.HISTORICAL,
                resolved_reference_date=None,
                inferred_from_question=True,
                period_year=year,
                period_start=p_start,
                period_end=p_end,
                warnings=(f"year_only_historical_period_detected:{year}:exact_date_not_invented",),
                missing_information=(f"exact_as_of_date_within_period:{year}",),
            )

    if _QUESTION_HISTORICAL_VERB_RE.search(q) and not re.search(
        r"(?i)\b(current|currently|today|now|recent(ly)?)\b", q
    ):
        # Historical wording without any date at all -> keep current mode if no date,
        # unless wording explicitly asks "previously"/"what was" (handled by retrieval_policy).
        pass

    return QuestionTemporalIntent(
        temporal_mode=TemporalQueryMode.CURRENT,
        resolved_reference_date=today,
    )
