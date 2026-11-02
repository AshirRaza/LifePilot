"""Non-destructive language/redirect remediation proposals."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from lifepilot.coverage.inventory import ExtractionView, detect_au_gte_redirect, detect_nl_language_mismatch


@dataclass
class MetadataRemediationProposal:
    kind: str
    document_id: int | None
    version_id: int | None
    source_id: int | None
    summary: str
    proposed_action: str
    do_not: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_metadata_proposals(
    views: list[ExtractionView],
) -> list[MetadataRemediationProposal]:
    proposals: list[MetadataRemediationProposal] = []

    for view in views:
        if detect_nl_language_mismatch(view):
            proposals.append(
                MetadataRemediationProposal(
                    kind="nl_language_mismatch",
                    document_id=view.document_id,
                    version_id=view.version_id,
                    source_id=view.source_id,
                    summary=(
                        f"Document {view.document_id} has doc_language={view.doc_language!r} "
                        f"but version_language={view.version_language!r} for URL {view.canonical_url}"
                    ),
                    proposed_action=(
                        "Add a non-destructive reconciliation note / metadata overlay: "
                        "keep historical document.language; record detected content language "
                        "on the version (or a side-table) for NL IND PDFs under /en/ paths."
                    ),
                    do_not=[
                        "Do not rewrite historical document_versions rows in place",
                        "Do not alter provenance or content_sha256",
                        "Do not re-fetch solely to flip language labels",
                    ],
                    evidence={
                        "canonical_url": view.canonical_url,
                        "source_url": view.source_url,
                        "doc_language": view.doc_language,
                        "version_language": view.version_language,
                    },
                )
            )

    au_views = [v for v in views if v.country == "AU" and v.source_id == 11023]
    for note in detect_au_gte_redirect(au_views):
        # Attach to first matching view if possible
        doc_id = None
        ver_id = None
        for v in au_views:
            blob = f"{v.canonical_url} {v.source_url} {v.title}".lower()
            if "genuine-temporary-entrant" in blob or "genuine temporary entrant" in blob:
                doc_id = v.document_id
                ver_id = v.version_id
                break
        proposals.append(
            MetadataRemediationProposal(
                kind="au_student_gte_redirect",
                document_id=doc_id,
                version_id=ver_id,
                source_id=11023,
                summary=note,
                proposed_action=(
                    "Record redirect provenance as additive metadata: store observed "
                    "legacy GTE URL and any known Genuine Student canonical destination "
                    "without overwriting source_url/canonical_url on historical versions. "
                    "Prefer a redirect_observations table or document note JSON."
                ),
                do_not=[
                    "Do not rewrite historical versions or URLs",
                    "Do not invent final_url if not stored",
                    "Do not fetch redirects in this phase",
                ],
                evidence={"note": note},
            )
        )

    if not any(p.kind == "au_student_gte_redirect" for p in proposals):
        proposals.append(
            MetadataRemediationProposal(
                kind="au_student_gte_redirect",
                document_id=None,
                version_id=None,
                source_id=11023,
                summary=(
                    "No stored final_url field; GTE→Genuine Student redirect provenance "
                    "remains unevaluated from current columns."
                ),
                proposed_action=(
                    "When a future fetch observes a redirect, append redirect observation "
                    "metadata; do not backfill by mutating prior versions."
                ),
                do_not=[
                    "Do not rewrite historical versions",
                    "Do not fabricate canonical destinations",
                ],
                evidence={},
            )
        )

    return proposals
