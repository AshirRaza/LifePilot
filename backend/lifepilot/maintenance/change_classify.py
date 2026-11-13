"""Conservative change classification for official-source rechecks."""

from __future__ import annotations

import re
from enum import Enum


class ChangeClass(str, Enum):
    UNCHANGED = "unchanged"
    EDITORIAL = "editorial"
    POTENTIALLY_MATERIAL = "potentially_material"
    UNCERTAIN = "uncertain"
    FETCH_FAILED = "fetch_failed"


_MATERIAL_RE = re.compile(
    r"(?i)\b("
    r"salary|salaries|remuneration|wage|threshold|fee|fees|surcharge|"
    r"eligib|exempt|exemption|requirement|criteria|maintenance|"
    r"effective\s+(from|date)|comes?\s+into\s+(force|effect)|"
    r"work\s+(right|authori[sz]ation)|permanent\s+residence|"
    r"€\s?\d|£\s?\d|\$\s?\d|\d[\d,]*\s*(euro|euros|gbp|usd)"
    r")\b"
)

_EDITORIAL_ONLY_HINTS = re.compile(
    r"(?i)\b(cookie|privacy\s+notice|accessibility|contact\s+us|"
    r"share\s+this|newsletter|breadcrumb|menu|footer|header)\b"
)

_MONEY_RE = re.compile(r"(?:£|\$|€)\s?\d[\d,]*(?:\.\d+)?")


def classify_content_change(
    *,
    previous_text: str | None,
    new_text: str | None,
    fetch_ok: bool,
    hashes_equal: bool | None = None,
) -> ChangeClass:
    """Classify a recheck. Prefer conservative labels when unsure."""
    if not fetch_ok:
        return ChangeClass.FETCH_FAILED
    if hashes_equal is True:
        return ChangeClass.UNCHANGED
    prev = (previous_text or "").strip()
    new = (new_text or "").strip()
    if not new:
        return ChangeClass.UNCERTAIN
    if prev and new and prev == new:
        return ChangeClass.UNCHANGED
    if not prev:
        # First capture into comparison context — treat conservatively.
        return ChangeClass.UNCERTAIN

    prev_money = set(_MONEY_RE.findall(prev))
    new_money = set(_MONEY_RE.findall(new))
    if prev_money != new_money:
        return ChangeClass.POTENTIALLY_MATERIAL

    # Diff window: look at lines that appear only on one side.
    prev_lines = {ln.strip() for ln in prev.splitlines() if ln.strip()}
    new_lines = {ln.strip() for ln in new.splitlines() if ln.strip()}
    added = new_lines - prev_lines
    removed = prev_lines - new_lines
    delta = "\n".join(sorted(added | removed))
    if not delta:
        return ChangeClass.EDITORIAL

    if _MATERIAL_RE.search(delta):
        return ChangeClass.POTENTIALLY_MATERIAL

    # Large non-material deltas still uncertain.
    if len(delta) > 2000 and not _EDITORIAL_ONLY_HINTS.search(delta):
        return ChangeClass.UNCERTAIN

    if _EDITORIAL_ONLY_HINTS.search(delta) and not _MATERIAL_RE.search(delta):
        return ChangeClass.EDITORIAL

    # Default: cannot confidently call editorial.
    if len(added) + len(removed) <= 3 and len(delta) < 400:
        return ChangeClass.EDITORIAL
    return ChangeClass.UNCERTAIN
