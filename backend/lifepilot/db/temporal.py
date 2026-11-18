"""Shared deterministic temporal metadata parsing, provenance, and precedence.

All parsed timestamps are normalized to timezone-aware UTC ``datetime`` objects
while retaining original raw values, extraction methods, source keys, and
explicit conflict records.

Observed HTTP headers, HTML metadata, and PDF file timestamps are NEVER
treated as authoritative legal effective dates.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable

_MIN_YEAR = 1980
_MAX_YEAR = 2100

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

_ISO_DATE_PREFIX_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?"
    r"(Z|[+-]\d{2}:?\d{2})?)?$"
)
_SLASH_YMD_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})$")
_DMY_MONTH_RE = re.compile(
    r"^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*(UTC|GMT|Z))?)?$",
    re.IGNORECASE,
)
_MDY_MONTH_RE = re.compile(
    r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*(UTC|GMT|Z))?)?$",
    re.IGNORECASE,
)
_PDF_DATE_RE = re.compile(
    r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    r"(Z00'00'|Z|[+-]\d{2}'?\d{2}'?)?$",
    re.IGNORECASE,
)

# Lower tuple rank = higher precedence for deterministic selection.
_METHOD_RANK: dict[str, int] = {
    "html_json_ld": 1,
    "html_meta": 2,
    "html_time_element": 3,
    "html_gov_banner": 4,
    "pdf_info_dict": 5,
    "http_header": 6,
}

_META_FIELD_RANK: dict[str, int] = {
    "meta:govuk:first-published-at": 1,
    "meta:article:published_time": 2,
    "meta:og:article:published_time": 2,
    "meta:dcterms.issued": 3,
    "meta:dc.date.issued": 3,
    "meta:publication_date": 4,
    "meta:publishdate": 4,
    "meta:pubdate": 4,
    "meta:dcterms.created": 5,
    "meta:dc.date": 6,
    "meta:date": 7,
    "meta:govuk:updated-at": 1,
    "meta:article:modified_time": 2,
    "meta:og:updated_time": 2,
    "meta:article:modified": 2,
    "meta:dcterms.modified": 3,
    "meta:dc.date.modified": 3,
    "meta:last-modified": 4,
    "meta:revised": 5,
}


@dataclass(frozen=True)
class TemporalDateObservation:
    """Single observed date/timestamp with full provenance."""

    kind: str  # published | updated | http_last_modified | pdf_created | pdf_modified
    extraction_method: str
    source_field: str
    raw_value: str
    normalized_utc: str | None = None
    status: str = "ok"  # ok | ok_date_only | ok_naive_assumed_utc | malformed | ambiguous | conflicting
    reliability: str = "meta_medium"
    is_file_metadata: bool = False
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HttpTemporalMetadata:
    """Parsed HTTP temporal/caching observations (never legal effective dates)."""

    retrieved_at: datetime
    http_last_modified: datetime | None = None
    http_last_modified_raw: str | None = None
    http_etag: str | None = None
    warnings: list[str] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TemporalResolutionResult:
    """Deterministic temporal resolution with explicit conflict reporting."""

    source_published_at: datetime | None = None
    source_updated_at: datetime | None = None
    file_created_at: datetime | None = None
    file_modified_at: datetime | None = None
    published_provenance: dict[str, Any] | None = None
    updated_provenance: dict[str, Any] | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    def to_storage_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_published_at": (
                self.source_published_at.isoformat()
                if self.source_published_at
                else None
            ),
            "source_updated_at": (
                self.source_updated_at.isoformat()
                if self.source_updated_at
                else None
            ),
            "file_created_at": (
                self.file_created_at.isoformat() if self.file_created_at else None
            ),
            "file_modified_at": (
                self.file_modified_at.isoformat() if self.file_modified_at else None
            ),
            "published_provenance": self.published_provenance,
            "updated_provenance": self.updated_provenance,
            "observations": list(self.observations),
            "conflicts": list(self.conflicts),
            "has_conflicts": self.has_conflicts,
            "note": (
                "Observed source/file timestamps only; not authoritative legal "
                "effective dates."
            ),
        }
        return payload


def _valid_year(dt: datetime) -> bool:
    return _MIN_YEAR <= dt.year <= _MAX_YEAR


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_tz_offset(raw_tz: str | None) -> timezone | None:
    if not raw_tz:
        return None
    tz = raw_tz.strip().upper().replace("'", "").replace(":", "")
    if tz in {"Z", "Z0000", "UTC", "GMT"}:
        return timezone.utc
    m = re.fullmatch(r"([+-])(\d{2})(\d{2})", tz)
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    hours = int(m.group(2))
    mins = int(m.group(3))
    if hours > 23 or mins > 59:
        return None
    return timezone(sign * timedelta(hours=hours, minutes=mins))


def parse_http_datetime(value: str | None) -> tuple[datetime | None, str | None]:
    """Parse an HTTP-date (RFC 7231 / RFC 850 / asctime / ISO) into UTC."""
    raw = (value or "").strip()
    if not raw:
        return None, "empty"
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            dt_utc = _to_utc(dt)
            if not _valid_year(dt_utc):
                return None, "year_out_of_range"
            return dt_utc, None
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    # Conservative ISO fallback (some proxies emit ISO timestamps in headers).
    dt_iso, err, _status = parse_source_datetime(raw)
    if dt_iso is not None:
        return dt_iso, None
    return None, err or "malformed_http_date"


def normalize_http_etag(value: str | None) -> str | None:
    """Normalize HTTP ETag (preserve quotes and W/ prefix; never parse as date)."""
    cleaned = (value or "").strip()
    return cleaned or None


def _iter_header_items(
    headers: Iterable[tuple[str, str]] | Any,
) -> list[tuple[str, str]]:
    if headers is None:
        return []
    multi = getattr(headers, "multi_items", None)
    if callable(multi):
        return [(str(k), str(v)) for k, v in multi()]
    if isinstance(headers, dict):
        return [(str(k), str(v)) for k, v in headers.items()]
    return [(str(k), str(v)) for k, v in headers]


def extract_http_header_temporal(
    headers: Iterable[tuple[str, str]] | Any,
    *,
    retrieved_at: datetime | None = None,
) -> HttpTemporalMetadata:
    """Extract Last-Modified & ETag safely without failing on malformed headers."""
    ret = _to_utc(retrieved_at or datetime.now(timezone.utc))
    out = HttpTemporalMetadata(retrieved_at=ret)
    items = _iter_header_items(headers)

    lm_values: list[str] = []
    etag_values: list[str] = []
    for k, v in items:
        kl = k.strip().lower()
        if kl == "last-modified":
            val = v.strip()
            if val and val not in lm_values:
                lm_values.append(val)
        elif kl == "etag":
            val = normalize_http_etag(v)
            if val and val not in etag_values:
                etag_values.append(val)

    if len(lm_values) > 1:
        out.warnings.append(
            f"conflicting_http_last_modified_headers:{len(lm_values)}"
        )
        for raw in lm_values:
            dt, err = parse_http_datetime(raw)
            out.observations.append(
                TemporalDateObservation(
                    kind="http_last_modified",
                    extraction_method="http_header",
                    source_field="Last-Modified",
                    raw_value=raw,
                    normalized_utc=dt.isoformat() if dt else None,
                    status="conflicting" if dt else (err or "malformed"),
                    reliability="meta_medium",
                    notes="Multiple conflicting Last-Modified headers received.",
                ).to_dict()
            )
    elif len(lm_values) == 1:
        raw = lm_values[0]
        out.http_last_modified_raw = raw
        dt, err = parse_http_datetime(raw)
        if dt is None:
            out.warnings.append(f"malformed_http_last_modified:{err}")
            out.observations.append(
                TemporalDateObservation(
                    kind="http_last_modified",
                    extraction_method="http_header",
                    source_field="Last-Modified",
                    raw_value=raw,
                    normalized_utc=None,
                    status="malformed",
                    reliability="unparsed",
                    notes=err,
                ).to_dict()
            )
        else:
            status = "ok"
            note = None
            if dt > ret + timedelta(days=1):
                status = "future_clock_skew"
                note = "HTTP Last-Modified is more than 1 day ahead of retrieved_at."
                out.warnings.append("http_last_modified_future_clock_skew")
            out.http_last_modified = dt
            out.observations.append(
                TemporalDateObservation(
                    kind="http_last_modified",
                    extraction_method="http_header",
                    source_field="Last-Modified",
                    raw_value=raw,
                    normalized_utc=dt.isoformat(),
                    status=status,
                    reliability="meta_medium",
                    notes=note,
                ).to_dict()
            )

    if len(etag_values) > 1:
        out.warnings.append(f"conflicting_http_etag_headers:{len(etag_values)}")
    elif len(etag_values) == 1:
        out.http_etag = etag_values[0]

    return out


def parse_source_datetime(
    value: str | None,
) -> tuple[datetime | None, str | None, str]:
    """Parse unambiguous ISO, RFC, or named-month source dates to UTC.

    Returns ``(dt_utc, error_reason, status)``.
    Ambiguous numeric dates (e.g., ``04/05/2026``) are rejected rather than guessed.
    """
    raw = re.sub(r"\s+", " ", (value or "").strip())
    if not raw:
        return None, "empty", "malformed"

    m = _ISO_DATE_PREFIX_RE.match(raw)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if m.group(4) is None:
            try:
                dt = datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                return None, "invalid_calendar_date", "malformed"
            if not _valid_year(dt):
                return None, "year_out_of_range", "malformed"
            return dt, None, "ok_date_only"
        hour = int(m.group(4))
        minute = int(m.group(5))
        second = int(m.group(6) or 0)
        tz = _parse_tz_offset(m.group(7))
        status = "ok" if tz is not None else "ok_naive_assumed_utc"
        try:
            dt = datetime(
                year, month, day, hour, minute, second, tzinfo=tz or timezone.utc
            )
        except ValueError:
            return None, "invalid_datetime", "malformed"
        dt_utc = _to_utc(dt)
        if not _valid_year(dt_utc):
            return None, "year_out_of_range", "malformed"
        return dt_utc, None, status

    sm = _SLASH_YMD_RE.match(raw)
    if sm:
        try:
            dt = datetime(
                int(sm.group(1)), int(sm.group(2)), int(sm.group(3)), tzinfo=timezone.utc
            )
        except ValueError:
            return None, "invalid_calendar_date", "malformed"
        if not _valid_year(dt):
            return None, "year_out_of_range", "malformed"
        return dt, None, "ok_date_only"

    for pat, order in ((_DMY_MONTH_RE, "dmy"), (_MDY_MONTH_RE, "mdy")):
        nm = pat.match(raw)
        if not nm:
            continue
        if order == "dmy":
            day = int(nm.group(1))
            mon_name = nm.group(2).lower()
            year = int(nm.group(3))
        else:
            mon_name = nm.group(1).lower()
            day = int(nm.group(2))
            year = int(nm.group(3))
        month = _MONTHS.get(mon_name)
        if month is None:
            return None, f"unknown_month:{mon_name}", "malformed"
        has_time = nm.group(4) is not None
        hour = int(nm.group(4) or 0)
        minute = int(nm.group(5) or 0)
        second = int(nm.group(6) or 0)
        try:
            dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        except ValueError:
            return None, "invalid_calendar_date", "malformed"
        if not _valid_year(dt):
            return None, "year_out_of_range", "malformed"
        if not has_time:
            return dt, None, "ok_date_only"
        return dt, None, ("ok" if nm.group(7) else "ok_naive_assumed_utc")

    # RFC 2822 fallback only if unambiguous weekday/month string.
    if re.search(r"[A-Za-z]{3}", raw):
        try:
            dt_rfc = parsedate_to_datetime(raw)
            if dt_rfc is not None:
                dt_utc = _to_utc(dt_rfc)
                if _valid_year(dt_utc):
                    return dt_utc, None, "ok"
                return None, "year_out_of_range", "malformed"
        except (TypeError, ValueError, IndexError, OverflowError):
            pass

    if re.match(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$", raw):
        return None, "ambiguous_numeric_date", "ambiguous"
    return None, "unrecognized_date_format", "malformed"


def parse_pdf_datetime(value: str | None) -> tuple[datetime | None, str | None, str]:
    """Parse PDF Info dictionary timestamps (``D:YYYYMMDDHHmmSS+HH'mm'`` or ISO)."""
    raw = (value or "").strip()
    if not raw:
        return None, "empty", "malformed"

    m = _PDF_DATE_RE.match(raw)
    if m:
        year = int(m.group(1))
        month = int(m.group(2) or 1)
        day = int(m.group(3) or 1)
        hour = int(m.group(4) or 0)
        minute = int(m.group(5) or 0)
        second = int(m.group(6) or 0)
        tz_raw = m.group(7)
        tz = _parse_tz_offset(tz_raw)
        if tz_raw and tz is None:
            return None, "invalid_pdf_timezone", "malformed"
        try:
            dt = datetime(
                year, month, day, hour, minute, second, tzinfo=tz or timezone.utc
            )
        except ValueError:
            return None, "invalid_pdf_datetime", "malformed"
        dt_utc = _to_utc(dt)
        if not _valid_year(dt_utc):
            return None, "year_out_of_range", "malformed"
        if m.group(4) is None:
            return dt_utc, None, "ok_date_only"
        return dt_utc, None, ("ok" if tz is not None else "ok_naive_assumed_utc")

    return parse_source_datetime(raw)


def _observation_sort_key(obs: TemporalDateObservation) -> tuple[int, int, str]:
    return (
        _METHOD_RANK.get(obs.extraction_method, 99),
        _META_FIELD_RANK.get(obs.source_field, 50),
        obs.source_field,
    )


def resolve_temporal_observations(
    observations: Iterable[TemporalDateObservation],
) -> TemporalResolutionResult:
    """Apply deterministic precedence and record any date conflicts."""
    obs_list = list(observations)
    res = TemporalResolutionResult(
        observations=[o.to_dict() for o in obs_list],
    )

    def _pick(
        kind: str,
    ) -> tuple[datetime | None, dict[str, Any] | None]:
        candidates: list[tuple[TemporalDateObservation, datetime]] = []
        for o in obs_list:
            if o.kind != kind or not o.normalized_utc:
                continue
            try:
                dt = _to_utc(datetime.fromisoformat(o.normalized_utc))
            except ValueError:
                continue
            candidates.append((o, dt))
        if not candidates:
            return None, None

        candidates.sort(key=lambda pair: _observation_sort_key(pair[0]))
        winner_obs, winner_dt = candidates[0]
        distinct_dates = {dt.date().isoformat() for _, dt in candidates}
        conflict = None
        if len(distinct_dates) > 1:
            conflict = {
                "kind": kind,
                "reason": "conflicting_source_dates",
                "selected_field": winner_obs.source_field,
                "selected_method": winner_obs.extraction_method,
                "selected_utc": winner_obs.normalized_utc,
                "candidates": [
                    {
                        "extraction_method": o.extraction_method,
                        "source_field": o.source_field,
                        "raw_value": o.raw_value,
                        "normalized_utc": o.normalized_utc,
                    }
                    for o, _ in candidates
                ],
            }
            res.conflicts.append(conflict)

        prov = {
            **winner_obs.to_dict(),
            "has_conflict": conflict is not None,
        }
        return winner_dt, prov

    res.source_published_at, res.published_provenance = _pick("published")
    res.source_updated_at, res.updated_provenance = _pick("updated")
    res.file_created_at, _ = _pick("pdf_created")
    res.file_modified_at, _ = _pick("pdf_modified")

    if (
        res.source_published_at is not None
        and res.source_updated_at is not None
        and res.source_published_at.date() > res.source_updated_at.date()
    ):
        res.conflicts.append(
            {
                "kind": "published_vs_updated",
                "reason": "published_after_updated",
                "source_published_at": res.source_published_at.isoformat(),
                "source_updated_at": res.source_updated_at.isoformat(),
            }
        )
    if (
        res.file_created_at is not None
        and res.file_modified_at is not None
        and res.file_created_at > res.file_modified_at
    ):
        res.conflicts.append(
            {
                "kind": "pdf_created_vs_modified",
                "reason": "pdf_created_after_modified",
                "file_created_at": res.file_created_at.isoformat(),
                "file_modified_at": res.file_modified_at.isoformat(),
            }
        )
    return res


def merge_temporal_notes(
    existing_notes: str | None,
    updates: dict[str, Any],
) -> str | None:
    """Merge structured provenance notes into ``DocumentVersion.temporal_notes``."""
    clean_updates = {k: v for k, v in updates.items() if v not in (None, [], {})}
    if not clean_updates:
        return existing_notes

    base: dict[str, Any] = {}
    if existing_notes and existing_notes.strip():
        try:
            loaded = json.loads(existing_notes)
            if isinstance(loaded, dict):
                base = loaded
            else:
                base = {"legacy_note": existing_notes}
        except json.JSONDecodeError:
            base = {"legacy_note": existing_notes}

    # Append-only history for observed header/provenance changes.
    history = list(base.get("history") or [])
    new_events = clean_updates.pop("events", None)
    if isinstance(new_events, list):
        for ev in new_events:
            if ev not in history:
                history.append(ev)
    if history:
        base["history"] = history[-25:]

    for k, v in clean_updates.items():
        base[k] = v

    return json.dumps(base, ensure_ascii=False, sort_keys=True)
