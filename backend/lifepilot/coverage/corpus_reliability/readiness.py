"""Readiness audit for provisionally ready pathways.

Single authoritative calculation: provisional readiness requires every
applicable critical requirement to be COMPLETE or NOT_APPLICABLE with valid
supporting evidence. COMPLETE is never confused with human_verified.

Rejected human-review evidence cannot contribute to provisional readiness.
Never sets human_verified=true unless an explicit confirm decision exists.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lifepilot.coverage.corpus_reliability.human_review import (
    apply_reviews_to_readiness,
    load_pathway_confirmations,
    load_reviews,
    pathway_confirmations_path,
    pathway_is_human_verified,
    reviews_path,
)

AUDIT_PATHWAYS = [
    "gb_skilled_worker",
    "nl_highly_skilled_migrant",
    "nl_naturalisation",
    "sg_employment_pass",
    "ie_critical_skills_employment_permit",
    "ie_general_employment_permit",
]

CRITICAL_REQUIREMENTS = [
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "official_provenance",
]

IE_CHECKLIST_PATTERNS = [
    "checklist",
    "documents required",
    "supporting documents",
]


@dataclass
class ReadinessReview:
    pathway_id: str
    country: str
    was_provisionally_ready: bool
    is_provisionally_ready: bool
    decision: str  # retain / downgrade / requires_manual_review / not_applicable
    rationale: str
    evidence_summary: dict[str, str] = field(default_factory=dict)
    concerns: list[str] = field(default_factory=list)
    reviewed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    human_verified: bool = False


@dataclass
class ReadinessSummary:
    """Authoritative counts shared by CSV, CLI, and report."""

    total_audited: int
    retain: int
    downgrade: int
    requires_manual_review: int
    not_applicable: int
    provisionally_ready_pathways: list[str]
    human_verified_pathways: list[str]
    reviews: list[ReadinessReview]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_audited": self.total_audited,
            "retain": self.retain,
            "downgrade": self.downgrade,
            "requires_manual_review": self.requires_manual_review,
            "not_applicable": self.not_applicable,
            "provisionally_ready_pathways": self.provisionally_ready_pathways,
            "human_verified_pathways": self.human_verified_pathways,
            "human_verified_count": len(self.human_verified_pathways),
            "note": "COMPLETE != human_verified; human_verified requires explicit confirm decision",
        }


def _cls(row: dict[str, Any]) -> str:
    return row.get("classification") or row.get("classification_6h") or "UNEVALUATED"


def check_evidence_quality(
    evidence_rows: list[dict[str, Any]],
    pathway_id: str,
) -> dict[str, Any]:
    pathway_rows = [
        r for r in evidence_rows
        if r["pathway_id"] == pathway_id and r.get("entity_type") == "true_pathway"
    ]

    summary: dict[str, str] = {}
    concerns: list[str] = []

    for req in CRITICAL_REQUIREMENTS:
        req_rows = [r for r in pathway_rows if r["requirement"] == req]
        if not req_rows:
            summary[req] = "MISSING_ROW"
            concerns.append(f"{req}: No evidence row found")
            continue

        row = req_rows[0]
        cls = _cls(row)
        summary[req] = cls

        if row.get("human_review_decision") == "reject" or row.get("readiness_excluded_reason") == "human_rejected":
            concerns.append(f"{req}: human-rejected evidence cannot support readiness")
            continue

        if cls == "NOT_APPLICABLE":
            rationale = (row.get("not_applicable_rationale") or row.get("explanation") or row.get("rationale") or "").strip()
            if not rationale:
                concerns.append(f"{req}: NOT_APPLICABLE without explicit justification")
            continue

        if cls == "COMPLETE":
            passage = row.get("passage", "") or ""
            doc_id = row.get("document_id")
            if req == "official_provenance":
                # Provenance may be source-linked without a passage; still need a source or doc.
                if not doc_id and not row.get("evidence_source_ids") and not row.get("source_url"):
                    concerns.append(f"{req}: COMPLETE without document/source reference")
                elif not passage or len(passage) < 50:
                    concerns.append(
                        f"{req}: COMPLETE with insufficient passage ({len(passage or '')} chars); "
                        "requires manual review before treating provenance as settled"
                    )
            else:
                if not doc_id:
                    concerns.append(f"{req}: COMPLETE without document_id")
                if not passage or len(passage) < 50:
                    concerns.append(f"{req}: COMPLETE with insufficient passage ({len(passage or '')} chars)")
                if req in ("eligibility", "application_process"):
                    passage_lower = passage.lower()
                    if any(p in passage_lower for p in IE_CHECKLIST_PATTERNS):
                        if not any(
                            kw in passage_lower
                            for kw in ("criteria", "requirement", "eligible", "how to apply", "process")
                        ):
                            concerns.append(
                                f"{req}: Evidence may be checklist-docs-only "
                                "(checklist keywords without substantive content)"
                            )
        elif cls in ("MISSING", "UNEVALUATED", "PARTIAL", "BLOCKED"):
            concerns.append(f"{req}: Classification is {cls}")

    applicable_ok = all(s in {"COMPLETE", "NOT_APPLICABLE"} for s in summary.values())
    return {
        "summary": summary,
        "concerns": concerns,
        "all_complete": applicable_ok and not concerns,
        "has_concerns": len(concerns) > 0,
    }


def audit_ie_pathway(
    evidence_rows: list[dict[str, Any]],
    pathway_id: str,
) -> dict[str, Any]:
    """IE readiness must not rest solely on checklist PDFs for eligibility/process.

    required_documents may legitimately cite an official checklist PDF; eligibility
    and application_process must cite permit guidance with substance.
    """
    pathway_rows = [
        r for r in evidence_rows
        if r["pathway_id"] == pathway_id and r.get("entity_type") == "true_pathway"
    ]
    ie_concerns = []
    for row in pathway_rows:
        req = row.get("requirement")
        cls = _cls(row)
        passage = row.get("passage", "") or row.get("reason", "")
        evidence_basis = row.get("evidence_basis", "")
        if cls == "COMPLETE" and req in ("eligibility", "application_process"):
            passage_lower = (passage or "").lower()
            is_checklist_only = (
                "checklist" in passage_lower
                or evidence_basis in ("official_checklist_pdf", "phase_6h_pdf_checklist_upgrade")
            )
            has_substance = (
                "eligible" in passage_lower
                or "criteria" in passage_lower
                or "requirement" in passage_lower
                or "must have" in passage_lower
                or "need to" in passage_lower
                or "how to apply" in passage_lower
                or len(passage or "") > 200
            )
            if is_checklist_only and not has_substance:
                ie_concerns.append({
                    "requirement": req,
                    "issue": "checklist_docs_only",
                    "evidence_basis": evidence_basis,
                    "passage_length": len(passage or ""),
                })
    return {
        "pathway_id": pathway_id,
        "ie_concerns": ie_concerns,
        "is_checklist_docs_only": len(ie_concerns) > 0,
    }


def pathway_is_provisionally_ready(evidence_rows: list[dict[str, Any]], pathway_id: str) -> bool:
    quality = check_evidence_quality(evidence_rows, pathway_id)
    return bool(quality["all_complete"]) and not quality["has_concerns"]


def audit_readiness(
    evidence_rows: list[dict[str, Any]],
    provisionally_ready: list[str] | None = None,
    pathways_to_audit: list[str] | None = None,
    human_reviews: list[dict[str, Any]] | None = None,
    pathway_confirmations: list[dict[str, Any]] | None = None,
    runs_dir: Path | None = None,
) -> list[ReadinessReview]:
    """Audit readiness from one authoritative evidence state."""
    effective = evidence_rows
    if human_reviews:
        effective = apply_reviews_to_readiness(evidence_rows, human_reviews)

    audit_list = pathways_to_audit or AUDIT_PATHWAYS
    # Derive current provisional readiness from evidence when not supplied.
    derived_ready = [
        pid for pid in audit_list
        if pathway_is_provisionally_ready(effective, pid)
    ]
    prior_ready = set(provisionally_ready or derived_ready)

    confirmations = pathway_confirmations
    if confirmations is None and runs_dir is not None:
        confirmations = load_pathway_confirmations(pathway_confirmations_path(runs_dir))
    confirmations = confirmations or []

    reviews: list[ReadinessReview] = []

    for pid in audit_list:
        pathway_rows = [r for r in effective if r["pathway_id"] == pid]
        country = pathway_rows[0].get("country", "") if pathway_rows else ""
        quality = check_evidence_quality(effective, pid)
        is_ready = pathway_is_provisionally_ready(effective, pid)
        was_ready = pid in prior_ready

        ie_audit = audit_ie_pathway(effective, pid) if pid.startswith("ie_") else None

        # Pathway human_verified requires: every applicable critical requirement
        # explicitly confirmed AND a separate confirm-pathway decision.
        # Packet generation / automated assessment never sets this flag.
        human_verified = False
        if human_reviews or confirmations:
            human_verified = pathway_is_human_verified(
                pid,
                evidence_rows=effective,
                reviews=human_reviews or [],
                confirmations=confirmations,
                critical_requirements=CRITICAL_REQUIREMENTS,
            )

        if not was_ready and not is_ready:
            decision = "not_applicable"
            rationale = "Pathway is not provisionally ready under current evidence"
        elif quality["has_concerns"] or not is_ready:
            if ie_audit and ie_audit["is_checklist_docs_only"]:
                decision = "downgrade"
                rationale = (
                    "IE pathway eligibility/process evidence is checklist-docs-only: "
                    f"{[c['requirement'] for c in ie_audit['ie_concerns']]}. "
                    "Permit guidance with substance is required."
                )
            elif any("COMPLETE without document_id" in c for c in quality["concerns"]):
                decision = "downgrade"
                rationale = "COMPLETE cells lack document_id references"
            elif any("human-rejected" in c for c in quality["concerns"]):
                decision = "downgrade"
                rationale = "One or more critical cells were rejected in human review"
            elif any("insufficient passage" in c for c in quality["concerns"]):
                decision = "requires_manual_review"
                rationale = "Evidence passages are too short for automated verification"
            elif any("NOT_APPLICABLE without explicit justification" in c for c in quality["concerns"]):
                decision = "requires_manual_review"
                rationale = "NOT_APPLICABLE cells lack explicit justification"
            else:
                decision = "requires_manual_review"
                rationale = f"Concerns found: {'; '.join(quality['concerns'][:3])}"
        else:
            decision = "retain"
            rationale = "All critical requirements COMPLETE/N/A with valid supporting evidence"

        reviews.append(ReadinessReview(
            pathway_id=pid,
            country=country,
            was_provisionally_ready=was_ready,
            is_provisionally_ready=is_ready and decision == "retain",
            decision=decision,
            rationale=rationale,
            evidence_summary=quality["summary"],
            concerns=quality["concerns"],
            human_verified=human_verified,
        ))

    return reviews


def summarize_readiness(reviews: list[ReadinessReview]) -> ReadinessSummary:
    counts = {"retain": 0, "downgrade": 0, "requires_manual_review": 0, "not_applicable": 0}
    for r in reviews:
        counts[r.decision] = counts.get(r.decision, 0) + 1
    return ReadinessSummary(
        total_audited=len(reviews),
        retain=counts.get("retain", 0),
        downgrade=counts.get("downgrade", 0),
        requires_manual_review=counts.get("requires_manual_review", 0),
        not_applicable=counts.get("not_applicable", 0),
        provisionally_ready_pathways=[r.pathway_id for r in reviews if r.decision == "retain"],
        human_verified_pathways=[r.pathway_id for r in reviews if r.human_verified],
        reviews=reviews,
    )


def write_readiness_csv(reviews: list[ReadinessReview], path: Path) -> None:
    fieldnames = [
        "pathway_id", "country", "was_provisionally_ready", "is_provisionally_ready",
        "decision", "rationale", "concerns", "evidence_summary",
        "reviewed_at", "human_verified",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in reviews:
            writer.writerow({
                "pathway_id": r.pathway_id,
                "country": r.country,
                "was_provisionally_ready": r.was_provisionally_ready,
                "is_provisionally_ready": r.is_provisionally_ready,
                "decision": r.decision,
                "rationale": r.rationale,
                "concerns": "; ".join(r.concerns),
                "evidence_summary": str(r.evidence_summary),
                "reviewed_at": r.reviewed_at,
                "human_verified": r.human_verified,
            })


def generate_human_review_packet(
    reviews: list[ReadinessReview],
    evidence_rows: list[dict[str, Any]],
) -> str:
    summary = summarize_readiness(reviews)
    lines = [
        "# Readiness Human Review Packet",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Authoritative Summary",
        "",
        f"- Audited: {summary.total_audited}",
        f"- Retain (provisionally ready): {summary.retain}",
        f"- Downgrade: {summary.downgrade}",
        f"- Requires manual review: {summary.requires_manual_review}",
        f"- Human-verified pathways: {len(summary.human_verified_pathways)} "
        f"({', '.join(summary.human_verified_pathways) or 'none'})",
        "",
        "COMPLETE is not human_verified. Human verification requires an explicit confirm decision.",
        "",
        "| Pathway | Decision | Concerns |",
        "|---------|----------|----------|",
    ]
    for r in reviews:
        concerns_short = "; ".join(r.concerns[:2]) if r.concerns else "None"
        lines.append(
            f"| {r.pathway_id} | {r.decision} | "
            f"{concerns_short[:50]}{'...' if len(concerns_short) > 50 else ''} |"
        )

    lines += ["", "## Pathways Requiring Review", ""]
    for r in reviews:
        if r.decision not in ("requires_manual_review", "downgrade"):
            continue
        lines.append(f"### {r.pathway_id} ({r.country})")
        lines.append("")
        lines.append(f"**Decision:** {r.decision}")
        lines.append(f"**Rationale:** {r.rationale}")
        lines.append("")
        lines.append("**Evidence Summary:**")
        for req, cls in r.evidence_summary.items():
            lines.append(f"- {req}: {cls}")
        lines.append("")
        if r.concerns:
            lines.append("**Concerns:**")
            for c in r.concerns:
                lines.append(f"- {c}")
            lines.append("")
        pathway_rows = [
            row for row in evidence_rows
            if row["pathway_id"] == r.pathway_id
            and row.get("entity_type") == "true_pathway"
            and _cls(row) == "COMPLETE"
        ]
        if pathway_rows:
            lines.append("**Evidence Passages:**")
            lines.append("")
            for row in pathway_rows:
                lines.append(f"**{row['requirement']}** (doc {row.get('document_id', 'N/A')}):")
                passage = (row.get("passage") or "")[:300]
                lines.append(f"> {passage}{'...' if len(row.get('passage') or '') > 300 else ''}")
                lines.append("")

    lines += [
        "---",
        "",
        "Use `python scripts/run_corpus_reliability.py review ...` to record confirm/reject/investigate.",
    ]
    return "\n".join(lines)


def load_human_reviews_for_runs(runs_dir: Path) -> list[dict[str, Any]]:
    return load_reviews(reviews_path(runs_dir))
