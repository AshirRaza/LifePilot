"""Deterministic locale-aware Decimal numeric, currency, and unit parser (Phase 10C.1).

Replaces naive string/comma stripping and bare-digit matching with explicit
Decimal arithmetic, conservative ambiguity handling, strict currency identity,
and period/unit verification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable, Sequence

from lifepilot.agent.evidence import EvidenceItem, EvidenceSourceType


class NumericParseStatus(str, Enum):
    PARSED = "parsed"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"


class NumericPeriodUnit(str, Enum):
    PER_YEAR = "per_year"
    PER_MONTH = "per_month"
    PER_WEEK = "per_week"
    PER_DAY = "per_day"
    PER_HOUR = "per_hour"
    ONE_TIME = "one_time"
    PERCENT = "percent"
    DURATION_YEARS = "duration_years"
    DURATION_MONTHS = "duration_months"
    DURATION_WEEKS = "duration_weeks"
    DURATION_DAYS = "duration_days"
    DURATION_HOURS = "duration_hours"
    POINTS = "points"


PERIOD_UNITS = frozenset(
    {
        NumericPeriodUnit.PER_YEAR,
        NumericPeriodUnit.PER_MONTH,
        NumericPeriodUnit.PER_WEEK,
        NumericPeriodUnit.PER_DAY,
        NumericPeriodUnit.PER_HOUR,
        NumericPeriodUnit.ONE_TIME,
    }
)
NON_ANNUAL_RATE_UNITS = frozenset(
    {
        NumericPeriodUnit.PER_MONTH,
        NumericPeriodUnit.PER_WEEK,
        NumericPeriodUnit.PER_DAY,
        NumericPeriodUnit.PER_HOUR,
    }
)
DURATION_UNITS = frozenset(
    {
        NumericPeriodUnit.DURATION_YEARS,
        NumericPeriodUnit.DURATION_MONTHS,
        NumericPeriodUnit.DURATION_WEEKS,
        NumericPeriodUnit.DURATION_DAYS,
        NumericPeriodUnit.DURATION_HOURS,
    }
)

_EU_LOCALE_HINTS = frozenset({"eu", "de", "nl", "fr", "es", "it", "pt", "pl"})
_ANGLO_LOCALE_HINTS = frozenset({"gb", "uk", "en", "en_gb", "en_us", "us", "ie", "sg", "ca", "au", "nz"})


@dataclass(frozen=True)
class ParsedNumericValue:
    """Canonical representation of one numeric/monetary/unit expression."""

    raw_literal: str
    number_token: str
    status: NumericParseStatus
    value: Decimal | None = None
    currency_code: str | None = None
    currency_symbol: str | None = None
    currency_ambiguous: bool = False
    currency_candidates: tuple[str, ...] = ()
    unit: NumericPeriodUnit | None = None
    duration_unit: str | None = None
    separator_convention: str | None = None
    ambiguity_reason: str | None = None
    start_char: int = 0
    end_char: int = 0
    is_year_like: bool = False
    is_date_component: bool = False

    @property
    def is_valid_parsed(self) -> bool:
        return self.status == NumericParseStatus.PARSED and self.value is not None


@dataclass(frozen=True)
class NumericMatchResult:
    matched: bool
    reason: str
    mismatch_kind: str | None = None


@dataclass(frozen=True)
class NumericSupportDiagnosis:
    supported: bool
    reason: str
    mismatch_kind: str | None = None
    claim_quantities: tuple[ParsedNumericValue, ...] = ()
    unmatched: tuple[tuple[ParsedNumericValue, str, str | None], ...] = ()


# ---------------------------------------------------------------------------
# Currency mappings
# ---------------------------------------------------------------------------

_ISO_CURRENCIES = frozenset(
    {
        "GBP",
        "EUR",
        "USD",
        "CAD",
        "AUD",
        "SGD",
        "NZD",
        "HKD",
        "CHF",
        "JPY",
        "CNY",
        "INR",
        "SEK",
        "NOK",
        "DKK",
        "PLN",
        "CZK",
        "EGP",
        "LBP",
    }
)

_PREFIX_SYMBOL_TO_ISO: dict[str, str] = {
    "£": "GBP",
    "€": "EUR",
    "₹": "INR",
    "US$": "USD",
    "CA$": "CAD",
    "C$": "CAD",
    "AU$": "AUD",
    "A$": "AUD",
    "SG$": "SGD",
    "S$": "SGD",
    "NZ$": "NZD",
    "HK$": "HKD",
    "JP¥": "JPY",
    "CN¥": "CNY",
    "RMB": "CNY",
    "CHF": "CHF",
    "FR.": "CHF",
}

_AMBIGUOUS_SYMBOL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "$": ("USD", "CAD", "AUD", "SGD", "NZD", "HKD"),
    "kr": ("SEK", "NOK", "DKK", "ISK"),
    "¥": ("JPY", "CNY"),
}

_CURRENCY_PREFIX_RE = (
    r"(?:"
    r"\b(?:US\$|CA\$|AU\$|SG\$|NZ\$|HK\$|JP¥|CN¥|S\$|C\$|A\$)|"
    r"\b(?:GBP|EUR|USD|CAD|AUD|SGD|NZD|HKD|CHF|JPY|CNY|RMB|INR|SEK|NOK|DKK|PLN|CZK|EGP|LBP)\b|"
    r"£|€|₹|¥|\$|\bkr\b"
    r")"
)
_CURRENCY_SUFFIX_RE = (
    r"(?:"
    r"\b(?:GBP|EUR|USD|CAD|AUD|SGD|NZD|HKD|CHF|JPY|CNY|RMB|INR|SEK|NOK|DKK|PLN|CZK|EGP|LBP)\b|"
    r"€|£|₹|¥|\bkr\b"
    r")"
)

_NUMBER_BODY_RE = (
    r"(?:"
    r"\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?(?![\d.,])|"
    r"\d+(?:[.,]\d+)+"
    r"|\d+"
    r")"
)

# Combined extractor:
#  1) Optional sign, currency prefix (+ optional bare $ after ISO like "CAD $55,000"), number, optional suffix ISO/symbol, optional %
#  2) Bare signed/unsigned number + optional suffix currency/symbol or %
_QUANTITY_RE = re.compile(
    rf"(?<![\w.])"
    rf"(?:(?P<sign1>[-−+])(?={_CURRENCY_PREFIX_RE}))?"
    rf"(?P<cur_pre>{_CURRENCY_PREFIX_RE}(?:\s*\$)?)\s*"
    rf"(?:(?P<sign2>[-−+])\s*)?"
    rf"(?P<num1>{_NUMBER_BODY_RE})"
    rf"(?:\s*(?P<cur_suf1>{_CURRENCY_SUFFIX_RE}))?"
    rf"(?:\s*(?P<pct1>%|per\s*cent|percent\b))?"
    rf"|"
    rf"(?<![\w.])"
    rf"(?:(?P<sign3>[-−+])(?=\d))?"
    rf"(?P<num2>{_NUMBER_BODY_RE})"
    rf"(?:\s*(?P<cur_suf2>{_CURRENCY_SUFFIX_RE}))?"
    rf"(?:\s*(?P<pct2>%|per\s*cent|percent\b))?",
    re.IGNORECASE,
)

# Date / non-quantity reference spans to ignore when extracting immigration amounts.
_ISO_DATE_SPAN_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_SLASH_DATE_SPAN_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
_MONTH_NAMES = (
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_DMY_DATE_SPAN_RE = re.compile(rf"(?i)\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTH_NAMES})\s+\d{{4}}\b")
_MDY_DATE_SPAN_RE = re.compile(rf"(?i)\b(?:{_MONTH_NAMES})\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b")
_DOC_LABEL_SPAN_RE = re.compile(
    r"(?i)\b(?:tier|phase|section|article|paragraph|clause|rule|chapter|part|annex|schedule|appendix|table|form|version|rev(?:ision)?|subclass)\s+\d+(?:\.\d+)*\b"
)

# Right-hand and left-hand unit patterns.
_COMPOUND_DURATION_PERIOD_RE = re.compile(
    r"^[-\s]*(?P<dur>hours?|hrs?)\s+(?:(?:per|a|/)\s*(?P<per>year|yr|month|mo|week|wk|day)|(?P<adv>yearly|annually|monthly|weekly|daily))\b",
    re.IGNORECASE,
)
_RIGHT_PERIOD_PATTERNS: tuple[tuple[re.Pattern[str], NumericPeriodUnit], ...] = (
    (re.compile(r"^\s*(?:\(?\s*(?:per\s+year|a\s+year|/\s*year|/\s*yr|per\s+annum|p\.?a\.?|yearly|annually)\s*\)?)\b", re.I), NumericPeriodUnit.PER_YEAR),
    (re.compile(r"^\s*(?:\(?\s*(?:per\s+month|a\s+month|/\s*month|/\s*mo|monthly)\s*\)?)\b", re.I), NumericPeriodUnit.PER_MONTH),
    (re.compile(r"^\s*(?:\(?\s*(?:per\s+week|a\s+week|/\s*week|/\s*wk|weekly)\s*\)?)\b", re.I), NumericPeriodUnit.PER_WEEK),
    (re.compile(r"^\s*(?:\(?\s*(?:per\s+day|a\s+day|/\s*day|daily)\s*\)?)\b", re.I), NumericPeriodUnit.PER_DAY),
    (re.compile(r"^\s*(?:\(?\s*(?:per\s+hour|an\s+hour|/\s*hour|/\s*hr|hourly)\s*\)?)\b", re.I), NumericPeriodUnit.PER_HOUR),
    (re.compile(r"^\s*(?:\(?\s*(?:one[-\s]time|per\s+application|single\s+fee|upfront)\s*\)?)\b", re.I), NumericPeriodUnit.ONE_TIME),
)
_RIGHT_PAREN_UNIT_RE = re.compile(
    r"^\s*\((yearly|annual|annually|monthly|weekly|daily|hourly|one[-_\s]time|per[_\s]year|per[_\s]month|per[_\s]week|per[_\s]day|per[_\s]hour|percent(?:age)?|points?|years?|months?|weeks?|days?|hours?)\)",
    re.IGNORECASE,
)
_RIGHT_DURATION_PATTERNS: tuple[tuple[re.Pattern[str], NumericPeriodUnit], ...] = (
    (re.compile(r"^[-\s]*(?:years?|yrs?)\b", re.I), NumericPeriodUnit.DURATION_YEARS),
    (re.compile(r"^[-\s]*(?:months?|mos?)\b", re.I), NumericPeriodUnit.DURATION_MONTHS),
    (re.compile(r"^[-\s]*(?:weeks?|wks?)\b", re.I), NumericPeriodUnit.DURATION_WEEKS),
    (re.compile(r"^[-\s]*(?:days?)\b", re.I), NumericPeriodUnit.DURATION_DAYS),
    (re.compile(r"^[-\s]*(?:hours?|hrs?)\b", re.I), NumericPeriodUnit.DURATION_HOURS),
    (re.compile(r"^[-\s]*(?:points?|point\b)", re.I), NumericPeriodUnit.POINTS),
)
_LEFT_PERIOD_PATTERNS: tuple[tuple[re.Pattern[str], NumericPeriodUnit], ...] = (
    (re.compile(r"(?i)\b(?:annual|annually|yearly|per\s+annum)\b"), NumericPeriodUnit.PER_YEAR),
    (re.compile(r"(?i)\bmonthly\b"), NumericPeriodUnit.PER_MONTH),
    (re.compile(r"(?i)\bweekly\b"), NumericPeriodUnit.PER_WEEK),
    (re.compile(r"(?i)\bdaily\b"), NumericPeriodUnit.PER_DAY),
    (re.compile(r"(?i)\bhourly\b"), NumericPeriodUnit.PER_HOUR),
    (re.compile(r"(?i)\b(?:one[-\s]time|single\s+fee|upfront)\b"), NumericPeriodUnit.ONE_TIME),
)

_LEGAL_RULE_UNIT_MAP: dict[str, NumericPeriodUnit] = {
    "yearly": NumericPeriodUnit.PER_YEAR,
    "annual": NumericPeriodUnit.PER_YEAR,
    "annually": NumericPeriodUnit.PER_YEAR,
    "per_year": NumericPeriodUnit.PER_YEAR,
    "monthly": NumericPeriodUnit.PER_MONTH,
    "per_month": NumericPeriodUnit.PER_MONTH,
    "weekly": NumericPeriodUnit.PER_WEEK,
    "per_week": NumericPeriodUnit.PER_WEEK,
    "daily": NumericPeriodUnit.PER_DAY,
    "per_day": NumericPeriodUnit.PER_DAY,
    "hourly": NumericPeriodUnit.PER_HOUR,
    "per_hour": NumericPeriodUnit.PER_HOUR,
    "one_time": NumericPeriodUnit.ONE_TIME,
    "one-time": NumericPeriodUnit.ONE_TIME,
    "per_application": NumericPeriodUnit.ONE_TIME,
    "percent": NumericPeriodUnit.PERCENT,
    "percentage": NumericPeriodUnit.PERCENT,
    "years": NumericPeriodUnit.DURATION_YEARS,
    "year": NumericPeriodUnit.DURATION_YEARS,
    "months": NumericPeriodUnit.DURATION_MONTHS,
    "month": NumericPeriodUnit.DURATION_MONTHS,
    "weeks": NumericPeriodUnit.DURATION_WEEKS,
    "week": NumericPeriodUnit.DURATION_WEEKS,
    "days": NumericPeriodUnit.DURATION_DAYS,
    "day": NumericPeriodUnit.DURATION_DAYS,
    "hours": NumericPeriodUnit.DURATION_HOURS,
    "hour": NumericPeriodUnit.DURATION_HOURS,
    "points": NumericPeriodUnit.POINTS,
    "point": NumericPeriodUnit.POINTS,
}


def _resolve_currency(
    prefix: str | None,
    suffix: str | None,
) -> tuple[str | None, str | None, bool, tuple[str, ...]]:
    """Resolve prefix/suffix currency markers conservatively."""
    tokens = [t.strip() for t in (prefix, suffix) if t and t.strip()]
    if not tokens:
        return None, None, False, ()

    iso_found: str | None = None
    sym_found: str | None = None
    ambig_sym: str | None = None

    for raw_tok in tokens:
        norm_sp = re.sub(r"\s+", "", raw_tok)
        upper = norm_sp.upper()
        # Handle "CAD$" or "$CAD" or explicit ISO code
        matched_iso = None
        for code in _ISO_CURRENCIES:
            if upper == code or upper.startswith(code) or upper.endswith(code):
                matched_iso = code
                break
        if matched_iso:
            if iso_found and iso_found != matched_iso:
                return None, raw_tok, True, (iso_found, matched_iso)
            iso_found = matched_iso
            sym_found = sym_found or matched_iso
            continue

        # Multi-char unambiguous symbols (US$, S$, C$, A$, etc.)
        if upper in _PREFIX_SYMBOL_TO_ISO:
            mapped = _PREFIX_SYMBOL_TO_ISO[upper]
            if iso_found and iso_found != mapped:
                return None, raw_tok, True, (iso_found, mapped)
            iso_found = mapped
            sym_found = norm_sp
            continue
        if norm_sp in _PREFIX_SYMBOL_TO_ISO:
            mapped = _PREFIX_SYMBOL_TO_ISO[norm_sp]
            if iso_found and iso_found != mapped:
                return None, raw_tok, True, (iso_found, mapped)
            iso_found = mapped
            sym_found = norm_sp
            continue

        low = norm_sp.lower()
        if norm_sp in _AMBIGUOUS_SYMBOL_CANDIDATES:
            ambig_sym = norm_sp
        elif low in _AMBIGUOUS_SYMBOL_CANDIDATES:
            ambig_sym = low

    if iso_found:
        return iso_found, sym_found or iso_found, False, ()
    if ambig_sym:
        return None, ambig_sym, True, _AMBIGUOUS_SYMBOL_CANDIDATES[ambig_sym]
    return None, sym_found, False, ()


def parse_numeric_token(
    token: str,
    *,
    locale_hint: str | None = None,
) -> tuple[NumericParseStatus, Decimal | None, str, str | None]:
    """Parse a raw numeric token (no currency) into Decimal without guessing.

    Returns (status, decimal_value, separator_convention, ambiguity_reason).
    """
    raw = (token or "").strip().replace("\u2212", "-").replace("\u00a0", " ").replace("\u202f", " ")
    if not raw:
        return NumericParseStatus.INVALID, None, "invalid", "empty_token"

    sign = ""
    if raw[0] in "+-":
        sign = "-" if raw[0] == "-" else ""
        raw = raw[1:].strip()
    if not raw or not re.fullmatch(r"[\d., ]+", raw):
        return NumericParseStatus.INVALID, None, "invalid", "invalid_characters"

    hint = (locale_hint or "").strip().lower() or None

    # 1. Space-separated thousands (e.g. "38 700", "38 700,50", "1 234 567.89")
    if " " in raw:
        if re.fullmatch(r"\d{1,3}(?: \d{3})+(?:[.,]\d+)?", raw):
            compact = raw.replace(" ", "")
            if "," in compact and "." in compact:
                return NumericParseStatus.INVALID, None, "invalid", "mixed_decimal_with_space_thousands"
            normalized = compact.replace(",", ".")
            try:
                return NumericParseStatus.PARSED, Decimal(f"{sign}{normalized}"), "space_thousands", None
            except InvalidOperation:
                return NumericParseStatus.INVALID, None, "invalid", "decimal_parse_failed"
        return NumericParseStatus.INVALID, None, "invalid", "malformed_space_grouping"

    has_dot = "." in raw
    has_comma = "," in raw

    # 2. Plain integer
    if not has_dot and not has_comma:
        try:
            return NumericParseStatus.PARSED, Decimal(f"{sign}{raw}"), "plain", None
        except InvalidOperation:
            return NumericParseStatus.INVALID, None, "invalid", "decimal_parse_failed"

    # 3. Both '.' and ',' present
    if has_dot and has_comma:
        last_dot = raw.rfind(".")
        last_comma = raw.rfind(",")
        if last_comma > last_dot:
            # European: 4.238,00 / 38.700,50 / 1.234.567,89
            int_part, frac_part = raw.rsplit(",", 1)
            if not frac_part or not frac_part.isdigit():
                return NumericParseStatus.INVALID, None, "invalid", "malformed_european_decimal"
            if not re.fullmatch(r"\d{1,3}(?:\.\d{3})+", int_part):
                return NumericParseStatus.INVALID, None, "invalid", "malformed_european_grouping"
            normalized = int_part.replace(".", "") + "." + frac_part
            try:
                return NumericParseStatus.PARSED, Decimal(f"{sign}{normalized}"), "european", None
            except InvalidOperation:
                return NumericParseStatus.INVALID, None, "invalid", "decimal_parse_failed"
        else:
            # Anglo/US: 4,238.00 / 38,700.50 / 1,234,567.89
            int_part, frac_part = raw.rsplit(".", 1)
            if not frac_part or not frac_part.isdigit():
                return NumericParseStatus.INVALID, None, "invalid", "malformed_anglo_decimal"
            if not re.fullmatch(r"\d{1,3}(?:,\d{3})+", int_part):
                return NumericParseStatus.INVALID, None, "invalid", "malformed_anglo_grouping"
            normalized = int_part.replace(",", "") + "." + frac_part
            try:
                return NumericParseStatus.PARSED, Decimal(f"{sign}{normalized}"), "anglo", None
            except InvalidOperation:
                return NumericParseStatus.INVALID, None, "invalid", "decimal_parse_failed"

    # 4. Dot(s) only
    if has_dot:
        dot_count = raw.count(".")
        if dot_count > 1:
            if re.fullmatch(r"\d{1,3}(?:\.\d{3}){2,}", raw):
                try:
                    return NumericParseStatus.PARSED, Decimal(f"{sign}{raw.replace('.', '')}"), "european_thousands", None
                except InvalidOperation:
                    return NumericParseStatus.INVALID, None, "invalid", "decimal_parse_failed"
            return NumericParseStatus.INVALID, None, "invalid", "multiple_dots_malformed"
        int_part, frac_part = raw.split(".", 1)
        if not int_part or not frac_part:
            return NumericParseStatus.INVALID, None, "invalid", "dangling_dot"
        if len(frac_part) == 3 and 1 <= len(int_part) <= 3 and int(int_part) > 0:
            if hint in _EU_LOCALE_HINTS:
                return NumericParseStatus.PARSED, Decimal(f"{sign}{int_part}{frac_part}"), "european_thousands", None
            if hint in _ANGLO_LOCALE_HINTS:
                return NumericParseStatus.PARSED, Decimal(f"{sign}{int_part}.{frac_part}"), "anglo_decimal", None
            return (
                NumericParseStatus.AMBIGUOUS,
                None,
                "ambiguous",
                "ambiguous_dot_three_digit_group",
            )
        try:
            return NumericParseStatus.PARSED, Decimal(f"{sign}{raw}"), "decimal_dot", None
        except InvalidOperation:
            return NumericParseStatus.INVALID, None, "invalid", "decimal_parse_failed"

    # 5. Comma(s) only
    comma_count = raw.count(",")
    if comma_count > 1:
        if re.fullmatch(r"\d{1,3}(?:,\d{3}){2,}", raw):
            try:
                return NumericParseStatus.PARSED, Decimal(f"{sign}{raw.replace(',', '')}"), "anglo_thousands", None
            except InvalidOperation:
                return NumericParseStatus.INVALID, None, "invalid", "decimal_parse_failed"
        return NumericParseStatus.INVALID, None, "invalid", "multiple_commas_malformed"

    int_part, frac_part = raw.split(",", 1)
    if not int_part or not frac_part:
        return NumericParseStatus.INVALID, None, "invalid", "dangling_comma"
    if len(frac_part) != 3 or int(int_part) == 0 or len(int_part) > 3:
        # e.g. 423,00 / 38,5 / 0,500 / 1234,50 -> unambiguous decimal comma
        try:
            return NumericParseStatus.PARSED, Decimal(f"{sign}{int_part}.{frac_part}"), "european_decimal", None
        except InvalidOperation:
            return NumericParseStatus.INVALID, None, "invalid", "decimal_parse_failed"

    # Single comma + 3 digits (e.g. 38,700)
    if hint in _EU_LOCALE_HINTS:
        return (
            NumericParseStatus.AMBIGUOUS,
            None,
            "ambiguous",
            "ambiguous_comma_three_digit_group_in_eu_locale",
        )
    try:
        return NumericParseStatus.PARSED, Decimal(f"{sign}{int_part}{frac_part}"), "anglo_thousands", None
    except InvalidOperation:
        return NumericParseStatus.INVALID, None, "invalid", "decimal_parse_failed"


def _protected_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pat in (
        _ISO_DATE_SPAN_RE,
        _SLASH_DATE_SPAN_RE,
        _DMY_DATE_SPAN_RE,
        _MDY_DATE_SPAN_RE,
        _DOC_LABEL_SPAN_RE,
    ):
        for m in pat.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


def _inside_spans(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in spans)


def _extract_unit_context(
    text: str,
    *,
    match_start: int,
    match_end: int,
    has_currency: bool,
    has_percent: bool,
    is_year_candidate: bool = False,
) -> tuple[NumericPeriodUnit | None, str | None]:
    if has_percent:
        return NumericPeriodUnit.PERCENT, None

    # Right window up to next sentence boundary or next digit (prevents stealing "3 years" after "£719 for up to 3 years").
    raw_right = text[match_end : match_end + 48]
    # Stop right scan before another distinct numeric token (except optional parenthesized unit).
    stop_match = re.search(r"[.!?;]|\d", raw_right)
    right = raw_right[: stop_match.start()] if stop_match else raw_right

    m_paren = _RIGHT_PAREN_UNIT_RE.match(right)
    if m_paren:
        key = m_paren.group(1).lower().replace("-", "_").replace(" ", "_")
        if key in _LEGAL_RULE_UNIT_MAP:
            return _LEGAL_RULE_UNIT_MAP[key], None

    m_comp = _COMPOUND_DURATION_PERIOD_RE.match(right)
    if m_comp:
        dur_raw = m_comp.group("dur").lower()
        dur_norm = "hours" if dur_raw.startswith(("hour", "hr")) else dur_raw
        per_raw = (m_comp.group("per") or m_comp.group("adv") or "").lower()
        per_map = {
            "year": NumericPeriodUnit.PER_YEAR,
            "yr": NumericPeriodUnit.PER_YEAR,
            "yearly": NumericPeriodUnit.PER_YEAR,
            "annually": NumericPeriodUnit.PER_YEAR,
            "month": NumericPeriodUnit.PER_MONTH,
            "mo": NumericPeriodUnit.PER_MONTH,
            "monthly": NumericPeriodUnit.PER_MONTH,
            "week": NumericPeriodUnit.PER_WEEK,
            "wk": NumericPeriodUnit.PER_WEEK,
            "weekly": NumericPeriodUnit.PER_WEEK,
            "day": NumericPeriodUnit.PER_DAY,
            "daily": NumericPeriodUnit.PER_DAY,
        }
        return per_map.get(per_raw), dur_norm

    for pat, unit in _RIGHT_PERIOD_PATTERNS:
        if pat.match(right):
            return unit, None

    if not has_currency:
        for pat, unit in _RIGHT_DURATION_PATTERNS:
            if pat.match(right):
                return unit, None

    # Left-hand period applies only to monetary / formatted amounts (never bare years like "annual threshold in 2020").
    if not is_year_candidate and (has_currency or not re.fullmatch(r"\d+", text[match_start:match_end].strip())):
        raw_left = text[max(0, match_start - 64) : match_start]
        clause_breaks = [m.end() for m in re.finditer(r"[.!?;,\n]|\d", raw_left)]
        left = raw_left[clause_breaks[-1] :] if clause_breaks else raw_left
        for pat, unit in _LEFT_PERIOD_PATTERNS:
            if pat.search(left):
                return unit, None

    return None, None


def extract_numeric_quantities(
    text: str,
    *,
    locale_hint: str | None = None,
    include_years: bool = False,
    include_bare_integers: bool = True,
) -> list[ParsedNumericValue]:
    """Extract all numeric/monetary/percentage/duration quantities with currency & unit metadata."""
    blob = text or ""
    if not blob.strip():
        return []
    spans = _protected_spans(blob)
    out: list[ParsedNumericValue] = []

    for m in _QUANTITY_RE.finditer(blob):
        start, end = m.start(), m.end()
        if _inside_spans(start, end, spans):
            continue

        if m.group("num1") is not None:
            sign = m.group("sign1") or m.group("sign2") or ""
            cur_pre = m.group("cur_pre")
            cur_suf = m.group("cur_suf1")
            num_tok = m.group("num1")
            has_pct = bool(m.group("pct1"))
        else:
            sign = m.group("sign3") or ""
            cur_pre = None
            cur_suf = m.group("cur_suf2")
            num_tok = m.group("num2")
            has_pct = bool(m.group("pct2"))

        signed_tok = f"{sign}{num_tok}" if sign else num_tok
        status, dec_val, sep_conv, ambig_reason = parse_numeric_token(
            signed_tok, locale_hint=locale_hint
        )
        cur_code, cur_sym, cur_ambig, cur_cands = _resolve_currency(cur_pre, cur_suf)
        has_currency = bool(cur_code or cur_ambig)

        # Bare integer calendar-year filter (e.g., 2020, 2026) when no currency/unit/separator.
        is_plain_four_digit_year = bool(
            not has_currency
            and not has_pct
            and sep_conv == "plain"
            and dec_val is not None
            and dec_val == dec_val.to_integral_value()
            and 1900 <= int(dec_val) <= 2100
        )

        unit, dur_unit = _extract_unit_context(
            blob,
            match_start=start,
            match_end=end,
            has_currency=has_currency,
            has_percent=has_pct,
            is_year_candidate=is_plain_four_digit_year,
        )

        if is_plain_four_digit_year and unit is None:
            if not include_years:
                continue

        if not include_bare_integers and not has_currency and not has_pct and unit is None and sep_conv == "plain":
            continue

        out.append(
            ParsedNumericValue(
                raw_literal=m.group(0).strip(),
                number_token=signed_tok,
                status=status,
                value=dec_val,
                currency_code=cur_code,
                currency_symbol=cur_sym,
                currency_ambiguous=cur_ambig,
                currency_candidates=cur_cands,
                unit=unit,
                duration_unit=dur_unit,
                separator_convention=sep_conv,
                ambiguity_reason=ambig_reason,
                start_char=start,
                end_char=end,
                is_year_like=is_plain_four_digit_year and unit is None,
            )
        )
    return out


def parse_single_quantity(
    literal: str,
    *,
    locale_hint: str | None = None,
) -> ParsedNumericValue | None:
    """Parse one standalone numeric literal/phrase into ParsedNumericValue."""
    found = extract_numeric_quantities(
        literal or "",
        locale_hint=locale_hint,
        include_years=True,
        include_bare_integers=True,
    )
    return found[0] if found else None


def quantities_from_evidence_item(
    item: EvidenceItem,
    *,
    locale_hint: str | None = None,
) -> list[ParsedNumericValue]:
    """Extract canonical numeric quantities from an EvidenceItem, preserving LegalRule precision."""
    eff_locale = locale_hint or getattr(item, "locale_hint", None)
    if eff_locale is None and getattr(item, "language", None):
        from lifepilot.agent.provenance import locale_hint_from_language

        eff_locale = locale_hint_from_language(item.language)
    out: list[ParsedNumericValue] = []
    if item.source_type == EvidenceSourceType.CURATED_RULE:
        prov = item.provenance or {}
        raw_val = prov.get("numeric_value")
        if raw_val is not None:
            try:
                dec = Decimal(str(raw_val))
                cur = str(prov.get("currency_code") or "").strip().upper() or None
                unit_raw = str(prov.get("amount_unit") or "").strip().lower()
                unit = _LEGAL_RULE_UNIT_MAP.get(unit_raw)
                out.append(
                    ParsedNumericValue(
                        raw_literal=f"{cur + ' ' if cur else ''}{dec}",
                        number_token=str(dec),
                        status=NumericParseStatus.PARSED,
                        value=dec,
                        currency_code=cur,
                        currency_symbol=cur,
                        unit=unit,
                        separator_convention="curated_decimal",
                    )
                )
            except InvalidOperation:
                pass
        raw_max = prov.get("numeric_max_value")
        if raw_max is not None:
            try:
                dec_max = Decimal(str(raw_max))
                cur = str(prov.get("currency_code") or "").strip().upper() or None
                unit_raw = str(prov.get("amount_unit") or "").strip().lower()
                unit = _LEGAL_RULE_UNIT_MAP.get(unit_raw)
                out.append(
                    ParsedNumericValue(
                        raw_literal=f"{cur + ' ' if cur else ''}{dec_max}",
                        number_token=str(dec_max),
                        status=NumericParseStatus.PARSED,
                        value=dec_max,
                        currency_code=cur,
                        currency_symbol=cur,
                        unit=unit,
                        separator_convention="curated_decimal",
                    )
                )
            except InvalidOperation:
                pass
    out.extend(
        extract_numeric_quantities(
            item.passage_text or "",
            locale_hint=eff_locale,
            include_years=False,
            include_bare_integers=True,
        )
    )
    return out


def quantity_matches_evidence(
    claim_q: ParsedNumericValue,
    ev_q: ParsedNumericValue,
) -> NumericMatchResult:
    """Compare one claim quantity against one evidence quantity strictly."""
    if claim_q.status != NumericParseStatus.PARSED or claim_q.value is None:
        return NumericMatchResult(
            False,
            claim_q.ambiguity_reason or "ambiguous_or_invalid_claim_number",
            "ambiguous",
        )
    if ev_q.status != NumericParseStatus.PARSED or ev_q.value is None:
        return NumericMatchResult(
            False,
            ev_q.ambiguity_reason or "ambiguous_or_invalid_evidence_number",
            "ambiguous",
        )

    if claim_q.value != ev_q.value:
        return NumericMatchResult(False, "numeric_value_mismatch", "value_mismatch")

    # Percentages must only match percentages.
    if (claim_q.unit == NumericPeriodUnit.PERCENT) != (
        ev_q.unit == NumericPeriodUnit.PERCENT
    ):
        return NumericMatchResult(False, "percent_unit_mismatch", "percent_mismatch")

    # Currency identity.
    if claim_q.currency_code:
        if ev_q.currency_code:
            if claim_q.currency_code != ev_q.currency_code:
                return NumericMatchResult(
                    False,
                    f"currency_mismatch:{claim_q.currency_code}!={ev_q.currency_code}",
                    "currency_mismatch",
                )
        elif ev_q.currency_ambiguous:
            return NumericMatchResult(
                False,
                f"ambiguous_currency_in_evidence:{ev_q.currency_symbol}",
                "ambiguous_currency",
            )
        else:
            return NumericMatchResult(
                False,
                "missing_currency_in_evidence",
                "missing_currency",
            )
    elif claim_q.currency_ambiguous:
        if ev_q.currency_code:
            return NumericMatchResult(
                False,
                f"ambiguous_currency_in_claim:{claim_q.currency_symbol}!={ev_q.currency_code}",
                "ambiguous_currency",
            )
        elif ev_q.currency_ambiguous:
            if claim_q.currency_symbol != ev_q.currency_symbol:
                return NumericMatchResult(
                    False,
                    f"currency_symbol_mismatch:{claim_q.currency_symbol}!={ev_q.currency_symbol}",
                    "currency_mismatch",
                )
        else:
            return NumericMatchResult(
                False,
                "missing_currency_in_evidence",
                "missing_currency",
            )
    elif ev_q.currency_code or ev_q.currency_ambiguous:
        # Claim omitted currency while evidence explicitly attaches a currency to this figure.
        if claim_q.unit not in DURATION_UNITS and claim_q.unit != NumericPeriodUnit.POINTS:
            return NumericMatchResult(
                False,
                "missing_currency_in_claim",
                "missing_currency",
            )

    # Duration unit check (e.g., "20 hours per week" vs "20 days per week").
    if claim_q.duration_unit and ev_q.duration_unit:
        if claim_q.duration_unit != ev_q.duration_unit:
            return NumericMatchResult(
                False,
                f"duration_unit_mismatch:{claim_q.duration_unit}!={ev_q.duration_unit}",
                "unit_mismatch",
            )
    elif bool(claim_q.duration_unit) != bool(ev_q.duration_unit):
        if claim_q.unit in PERIOD_UNITS or ev_q.unit in PERIOD_UNITS:
            return NumericMatchResult(
                False,
                "duration_vs_monetary_unit_mismatch",
                "unit_mismatch",
            )

    # Unit & period check.
    if claim_q.unit != ev_q.unit:
        if claim_q.unit is not None and ev_q.unit is not None:
            return NumericMatchResult(
                False,
                f"unit_period_mismatch:{claim_q.unit.value}!={ev_q.unit.value}",
                "unit_mismatch",
            )
        if claim_q.unit is not None and ev_q.unit is None:
            return NumericMatchResult(
                False,
                f"unit_unverified_in_evidence:{claim_q.unit.value}",
                "unit_mismatch",
            )
        if claim_q.unit is None and ev_q.unit in NON_ANNUAL_RATE_UNITS:
            return NumericMatchResult(
                False,
                f"missing_period_unit_in_claim:{ev_q.unit.value}",
                "unit_mismatch",
            )
        if claim_q.unit is None and ev_q.unit in DURATION_UNITS:
            return NumericMatchResult(
                False,
                f"missing_duration_unit_in_claim:{ev_q.unit.value}",
                "unit_mismatch",
            )

    return NumericMatchResult(True, "matched", None)


_MISMATCH_SEVERITY: dict[str | None, int] = {
    # Most informative failure when no candidate matched for a claim quantity.
    "unit_mismatch": 5,
    "currency_mismatch": 5,
    "percent_mismatch": 5,
    "ambiguous_currency": 4,
    "missing_currency": 4,
    "ambiguous": 3,
    "value_mismatch": 1,
}


def diagnose_claim_numeric_support(
    claim: str,
    evidence_texts_or_items: Sequence[str | EvidenceItem],
    *,
    locale_hint: str | None = None,
    include_years: bool = False,
) -> NumericSupportDiagnosis:
    """Verify every numeric claim quantity against cited passages/EvidenceItems."""
    claim_qs = extract_numeric_quantities(
        claim or "",
        locale_hint=locale_hint,
        include_years=include_years,
        include_bare_integers=True,
    )
    if not claim_qs:
        return NumericSupportDiagnosis(
            supported=True,
            reason="no_material_numeric_quantities",
            claim_quantities=(),
        )

    ev_qs: list[ParsedNumericValue] = []
    for entry in evidence_texts_or_items or ():
        if isinstance(entry, EvidenceItem):
            ev_qs.extend(quantities_from_evidence_item(entry, locale_hint=locale_hint))
        else:
            ev_qs.extend(
                extract_numeric_quantities(
                    str(entry or ""),
                    locale_hint=locale_hint,
                    include_years=include_years,
                    include_bare_integers=True,
                )
            )

    unmatched: list[tuple[ParsedNumericValue, str, str | None]] = []
    for cq in claim_qs:
        if cq.status != NumericParseStatus.PARSED:
            unmatched.append(
                (cq, cq.ambiguity_reason or "ambiguous_or_invalid_claim_number", "ambiguous")
            )
            continue

        matched = False
        best_reason = "numeric_literal_not_in_passage"
        best_kind: str | None = "value_mismatch"
        best_rank = -1
        for eq in ev_qs:
            res = quantity_matches_evidence(cq, eq)
            if res.matched:
                matched = True
                break
            rank = _MISMATCH_SEVERITY.get(res.mismatch_kind, 0)
            if eq.status == NumericParseStatus.AMBIGUOUS and eq.number_token == cq.number_token:
                rank = max(rank, 4)
            if rank > best_rank:
                best_rank = rank
                best_reason = res.reason
                best_kind = res.mismatch_kind
        if not matched:
            unmatched.append((cq, best_reason, best_kind))

    if unmatched:
        primary_reason = unmatched[0][1]
        primary_kind = unmatched[0][2]
        return NumericSupportDiagnosis(
            supported=False,
            reason=primary_reason,
            mismatch_kind=primary_kind,
            claim_quantities=tuple(claim_qs),
            unmatched=tuple(unmatched),
        )

    return NumericSupportDiagnosis(
        supported=True,
        reason="all_quantities_verified",
        claim_quantities=tuple(claim_qs),
    )


def format_canonical_text_literals(text: str) -> str:
    """Locale-aware string normalization preserving European decimal commas & ambiguous tokens.

    Unlike naive comma removal (which turns '€423,00' into '€42300'), this converts
    unambiguously parsed quantities to canonical Decimal strings (e.g. '€423.00',
    '£38700') while leaving ambiguous tokens such as '38.000' untouched.
    """
    blob = text or ""
    if not blob:
        return ""
    quantities = extract_numeric_quantities(
        blob, include_years=True, include_bare_integers=True
    )
    if not quantities:
        return re.sub(r"\s+", "", blob)

    pieces: list[str] = []
    cursor = 0
    for q in quantities:
        if q.start_char < cursor:
            continue
        pieces.append(re.sub(r"[\s,]", "", blob[cursor : q.start_char]))
        if q.status == NumericParseStatus.PARSED and q.value is not None:
            sym = q.currency_symbol or (q.currency_code or "")
            pct = "%" if q.unit == NumericPeriodUnit.PERCENT else ""
            pieces.append(f"{sym}{q.value}{pct}")
        else:
            pieces.append(re.sub(r"\s+", "", q.raw_literal))
        cursor = q.end_char
    pieces.append(re.sub(r"[\s,]", "", blob[cursor:]))
    return "".join(pieces)


def canonical_quantity_signature(q: ParsedNumericValue) -> tuple[str, str, str] | None:
    """Hashable canonical signature (decimal, currency, unit) for conflict/dedup checks."""
    if q.status != NumericParseStatus.PARSED or q.value is None:
        return None
    val_norm = format(q.value.normalize(), "f")
    if "." in val_norm:
        val_norm = val_norm.rstrip("0").rstrip(".") or "0"
    cur = q.currency_code or (q.currency_symbol or "")
    unit = q.unit.value if q.unit is not None else ""
    if q.duration_unit:
        unit = f"{q.duration_unit}:{unit}"
    return (val_norm, cur, unit)


def quantities_conflict(a: ParsedNumericValue, b: ParsedNumericValue) -> bool:
    """True when two parsed amounts express incompatible values, currencies, or units."""
    if a.status != NumericParseStatus.PARSED or b.status != NumericParseStatus.PARSED:
        return False
    if a.value is None or b.value is None:
        return False
    if a.value != b.value:
        return True
    if a.currency_code and b.currency_code and a.currency_code != b.currency_code:
        return True
    if a.unit is not None and b.unit is not None and a.unit != b.unit:
        return True
    if a.duration_unit and b.duration_unit and a.duration_unit != b.duration_unit:
        return True
    return False


def material_literals_for_scrub(text: str) -> list[str]:
    """Return all numeric tokens/literals in text suitable for prose leak scrubbing."""
    qs = extract_numeric_quantities(
        text or "", include_years=False, include_bare_integers=True
    )
    out: list[str] = []
    seen: set[str] = set()
    for q in qs:
        for tok in (q.raw_literal, q.number_token):
            t = tok.strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out
