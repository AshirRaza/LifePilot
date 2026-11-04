"""Lightweight CLI-backed human review for pathway evidence.

Machine assessment status stays separate from human verification.
Only an explicit reviewer decision can confirm, reject, or flag evidence.

Pathway-level human verification additionally requires an explicit
confirm-pathway decision AFTER every applicable critical requirement has
been confirmed. Packet generation and automated processing never set
human_verified.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

# confirm == approve; investigate == request_further_evidence
ReviewDecision = Literal["confirm", "reject", "investigate", "request_further_evidence", "approve"]

VALID_DECISIONS = {
    "confirm",
    "reject",
    "investigate",
    "request_further_evidence",
    "approve",
}

# Canonicalize aliases used by the Phase 6R reviewer CLI.
DECISION_ALIASES = {
    "approve": "confirm",
    "request_further_evidence": "investigate",
}

PATHWAY_CONFIRM_REQUIREMENT = "__pathway_confirm__"


@dataclass
class HumanReviewDecision:
    pathway_id: str
    requirement: str
    decision: str
    reviewer_id: str
    classification_at_review: str
    document_id: str = ""
    version_id: str = ""
    source_url: str = ""
    passage: str = ""
    rationale_at_review: str = ""
    explanation: str = ""
    reviewed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Cell-level flag only; pathway human_verified requires confirm-pathway.
    human_verified: bool = False

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


def reviews_path(runs_dir: Path) -> Path:
    return runs_dir / "human_evidence_reviews.csv"


def pathway_confirmations_path(runs_dir: Path) -> Path:
    return runs_dir / "human_pathway_confirmations.csv"


def load_reviews(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def append_review(path: Path, decision: HumanReviewDecision) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(decision.to_row().keys())
    exists = path.is_file() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow(decision.to_row())


def normalize_decision(decision: str) -> str:
    d = (decision or "").strip().lower()
    if d not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(VALID_DECISIONS)}")
    return DECISION_ALIASES.get(d, d)


def record_decision(
    *,
    runs_dir: Path,
    pathway_id: str,
    requirement: str,
    decision: str,
    reviewer_id: str,
    evidence_row: dict[str, Any] | None = None,
    explanation: str = "",
) -> HumanReviewDecision:
    if requirement == PATHWAY_CONFIRM_REQUIREMENT:
        raise ValueError(
            "Use confirm_pathway() for pathway-level confirmation; "
            "requirement-level review cannot set pathway human_verified."
        )
    canonical = normalize_decision(decision)
    if not reviewer_id.strip():
        raise ValueError("reviewer_id is required")

    row = evidence_row or {}
    cls = row.get("classification") or row.get("classification_6h") or ""
    recorded = HumanReviewDecision(
        pathway_id=pathway_id,
        requirement=requirement,
        decision=canonical,
        reviewer_id=reviewer_id.strip(),
        classification_at_review=cls,
        document_id=str(row.get("document_id") or ""),
        version_id=str(row.get("version_id") or ""),
        source_url=str(row.get("source_url") or row.get("url") or ""),
        passage=str(row.get("passage") or "")[:500],
        rationale_at_review=str(row.get("rationale") or row.get("explanation") or "")[:500],
        explanation=explanation.strip(),
        # Requirement confirm is not pathway human_verified.
        human_verified=False,
    )
    append_review(reviews_path(runs_dir), recorded)
    return recorded


def latest_decisions_by_cell(reviews: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Last decision wins per (pathway_id, requirement)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in reviews:
        if r.get("requirement") == PATHWAY_CONFIRM_REQUIREMENT:
            continue
        key = (r["pathway_id"], r["requirement"])
        out[key] = r
    return out


def load_pathway_confirmations(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def latest_pathway_confirmations(
    confirmations: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in confirmations:
        pid = row.get("pathway_id") or ""
        if not pid:
            continue
        out[pid] = row
    return out


def critical_requirements_confirmed(
    *,
    pathway_id: str,
    evidence_rows: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    critical_requirements: list[str],
) -> tuple[bool, list[str]]:
    """Return (all_confirmed, blocking_issues)."""
    latest = latest_decisions_by_cell(reviews)
    blocking: list[str] = []
    needed = 0
    confirms = 0
    pathway_rows = [r for r in evidence_rows if r.get("pathway_id") == pathway_id]
    for req in critical_requirements:
        cell = next((r for r in pathway_rows if r.get("requirement") == req), None)
        if not cell:
            continue
        cls = (cell.get("classification") or cell.get("classification_6h") or "").upper()
        if cls == "NOT_APPLICABLE":
            continue
        needed += 1
        if cls not in {"COMPLETE", "PARTIAL"}:
            blocking.append(f"{req}: classification={cls or 'empty'} (missing critical evidence)")
        rev = latest.get((pathway_id, req))
        if not rev:
            blocking.append(f"{req}: no human decision recorded")
            continue
        decision = rev.get("decision") or ""
        if decision == "reject":
            blocking.append(f"{req}: rejected by reviewer")
        elif decision == "investigate":
            blocking.append(f"{req}: further evidence requested")
        elif decision == "confirm":
            confirms += 1
        else:
            blocking.append(f"{req}: unrecognized decision={decision}")
    if needed == 0:
        blocking.append("no applicable critical requirements found")
        return False, blocking
    if confirms != needed:
        if not any("no human decision" in b or "further evidence" in b or "rejected" in b for b in blocking):
            blocking.append(f"confirmed {confirms}/{needed} applicable critical requirements")
    return (confirms == needed and not blocking), blocking


def confirm_pathway(
    *,
    runs_dir: Path,
    pathway_id: str,
    reviewer_id: str,
    evidence_rows: list[dict[str, Any]],
    critical_requirements: list[str],
    explanation: str = "",
    allow_incomplete: bool = False,
) -> dict[str, Any]:
    """Explicit pathway-level confirmation. Refuses bulk/incomplete bypass by default."""
    if not reviewer_id.strip():
        raise ValueError("reviewer_id is required")
    if allow_incomplete:
        raise ValueError(
            "Bulk or incomplete pathway confirmation is not permitted. "
            "Every applicable critical requirement must be explicitly confirmed first."
        )

    reviews = load_reviews(reviews_path(runs_dir))
    ok, blocking = critical_requirements_confirmed(
        pathway_id=pathway_id,
        evidence_rows=evidence_rows,
        reviews=reviews,
        critical_requirements=critical_requirements,
    )
    if not ok:
        return {
            "pathway_id": pathway_id,
            "accepted": False,
            "human_verified": False,
            "blocking_issues": blocking,
            "message": (
                "Pathway confirmation refused: unresolved requirement reviews or "
                "missing critical evidence. No bulk bypass is available."
            ),
        }

    row = {
        "pathway_id": pathway_id,
        "requirement": PATHWAY_CONFIRM_REQUIREMENT,
        "decision": "confirm_pathway",
        "reviewer_id": reviewer_id.strip(),
        "explanation": explanation.strip(),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "human_verified": True,
        "blocking_issues": "",
    }
    path = pathway_confirmations_path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(row.keys())
    exists = path.is_file() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow(row)

    # Also append an audit breadcrumb in the requirement review log (not a cell confirm).
    breadcrumb = HumanReviewDecision(
        pathway_id=pathway_id,
        requirement=PATHWAY_CONFIRM_REQUIREMENT,
        decision="confirm_pathway",
        reviewer_id=reviewer_id.strip(),
        classification_at_review="",
        explanation=explanation.strip(),
        human_verified=True,
    )
    append_review(reviews_path(runs_dir), breadcrumb)

    return {
        "pathway_id": pathway_id,
        "accepted": True,
        "human_verified": True,
        "blocking_issues": [],
        "message": f"Pathway {pathway_id} human-verified by {reviewer_id.strip()}",
        "confirmation": row,
    }


_DEFAULT_CRITICAL = (
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "official_provenance",
)


def pathway_is_human_verified(
    pathway_id: str,
    *,
    runs_dir: Path | None = None,
    evidence_rows: list[dict[str, Any]] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    confirmations: list[dict[str, Any]] | None = None,
    critical_requirements: list[str] | None = None,
) -> bool:
    """True only with per-requirement confirms + explicit pathway confirmation."""
    crit = list(critical_requirements or _DEFAULT_CRITICAL)
    if runs_dir is not None:
        reviews = reviews if reviews is not None else load_reviews(reviews_path(runs_dir))
        confirmations = (
            confirmations
            if confirmations is not None
            else load_pathway_confirmations(pathway_confirmations_path(runs_dir))
        )
    reviews = reviews or []
    confirmations = confirmations or []
    evidence_rows = evidence_rows or []

    latest_conf = latest_pathway_confirmations(confirmations).get(pathway_id)
    if not latest_conf or latest_conf.get("decision") != "confirm_pathway":
        return False
    hv = latest_conf.get("human_verified")
    if hv is not True and str(hv).lower() not in {"true", "1", "yes"}:
        return False

    ok, _blocking = critical_requirements_confirmed(
        pathway_id=pathway_id,
        evidence_rows=evidence_rows,
        reviews=reviews,
        critical_requirements=crit,
    )
    return ok


def apply_reviews_to_readiness(
    evidence_rows: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return evidence rows with rejected cells excluded from positive readiness claims.

    Rejected evidence is forced to UNEVALUATED for readiness calculation only when
    the review decision is reject. Confirm does not auto-upgrade classification.
    """
    latest = latest_decisions_by_cell(reviews)
    out: list[dict[str, Any]] = []
    for row in evidence_rows:
        r = dict(row)
        key = (r.get("pathway_id"), r.get("requirement"))
        rev = latest.get(key)  # type: ignore[arg-type]
        if rev and rev.get("decision") == "reject":
            r["classification"] = "UNEVALUATED"
            if "classification_6h" in r:
                r["classification_6h"] = "UNEVALUATED"
            r["human_review_decision"] = "reject"
            r["readiness_excluded_reason"] = "human_rejected"
        elif rev and rev.get("decision") == "confirm":
            r["human_review_decision"] = "confirm"
            # Cell confirmed — still not pathway human_verified.
            r["requirement_human_confirmed"] = True
        elif rev and rev.get("decision") == "investigate":
            r["human_review_decision"] = "investigate"
        out.append(r)
    return out


def format_cell_for_review(row: dict[str, Any]) -> str:
    cls = row.get("classification") or row.get("classification_6h") or ""
    lines = [
        f"Pathway: {row.get('pathway_id')}",
        f"Requirement: {row.get('requirement')}",
        f"Classification: {cls}",
        f"Document ID: {row.get('document_id') or '—'}",
        f"Version ID: {row.get('version_id') or '—'}",
        f"Source URL: {row.get('source_url') or row.get('url') or '—'}",
        f"Method/basis: {row.get('method') or row.get('evidence_basis') or row.get('evidence_repair') or '—'}",
        "",
        "Passage:",
        str(row.get("passage") or "(empty)"),
        "",
        "Rationale:",
        str(row.get("rationale") or row.get("explanation") or "(empty)"),
    ]
    return "\n".join(lines)


def write_reviews_snapshot(reviews: list[dict[str, Any]], path: Path) -> None:
    path.write_text(json.dumps(reviews, indent=2, ensure_ascii=False), encoding="utf-8")
