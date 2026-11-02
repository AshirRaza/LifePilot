"""Evidence relevance checks for critical requirement cells."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lifepilot.coverage.inventory import ExtractionView
from lifepilot.coverage.normalization.normalize import NormalizedEntity, load_normalization_config
from lifepilot.coverage.schema import CoverageClass, EvidenceRef, RequirementCell
from lifepilot.coverage.taxonomy import CRITICAL_FIELDS, is_critical_field

CRITICAL = frozenset(CRITICAL_FIELDS)


@dataclass
class RelevanceDecision:
    accepted: bool
    reason: str
    matched_identity: list[str] = field(default_factory=list)
    matched_reject: list[str] = field(default_factory=list)


def _profile_for(normalized_id: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or load_normalization_config()
    profiles = cfg.get("relevance_profiles") or {}
    return dict(profiles.get(normalized_id) or {})


def view_matches_pathway_identity(
    view: ExtractionView,
    entity: NormalizedEntity,
    *,
    config: dict[str, Any] | None = None,
) -> RelevanceDecision:
    """Reject evidence that is wrong country or lacks pathway identity tokens."""
    if view.country.upper() != entity.country.upper():
        return RelevanceDecision(
            accepted=False,
            reason=f"country mismatch: evidence={view.country} pathway={entity.country}",
        )

    if view.is_blocked_evidence:
        return RelevanceDecision(accepted=False, reason="blocked/rejected document excluded")

    profile = _profile_for(entity.normalized_id, config)
    blob = " ".join(
        [
            view.title,
            view.canonical_url,
            view.source_url,
            " ".join(view.headings[:30]),
            " ".join(view.paragraphs[:20]),
        ]
    ).lower()

    reject = [t.lower() for t in (profile.get("reject_tokens") or [])]
    matched_reject = [t for t in reject if t in blob]
    # Soft reject: only if reject tokens present AND identity tokens absent
    identity = [t.lower() for t in (profile.get("identity_tokens") or [])]
    matched_identity = [t for t in identity if t in blob]

    if matched_reject and not matched_identity:
        return RelevanceDecision(
            accepted=False,
            reason="reject tokens without pathway identity",
            matched_identity=matched_identity,
            matched_reject=matched_reject,
        )

    if identity and not matched_identity:
        # Document linked to pathway source but no identity signal → uncertain
        return RelevanceDecision(
            accepted=False,
            reason="missing pathway identity tokens in evidence",
            matched_identity=[],
            matched_reject=matched_reject,
        )

    return RelevanceDecision(
        accepted=True,
        reason="identity and country checks passed",
        matched_identity=matched_identity,
        matched_reject=matched_reject,
    )


def excerpt_relevant_for_field(
    excerpt: str,
    field: str,
    entity: NormalizedEntity,
    *,
    config: dict[str, Any] | None = None,
) -> RelevanceDecision:
    """Field-level relevance (esp. financial vs unrelated sponsorship)."""
    text = (excerpt or "").lower()
    profile = _profile_for(entity.normalized_id, config)

    if field == "financial_or_sponsorship":
        require = [t.lower() for t in (profile.get("financial_require_tokens") or [])]
        reject = [t.lower() for t in (profile.get("financial_reject_tokens") or [])]
        matched_rej = [t for t in reject if t in text]
        if matched_rej:
            return RelevanceDecision(
                accepted=False,
                reason="financial evidence matches reject context (e.g. diplomatic exemption)",
                matched_reject=matched_rej,
            )
        if require:
            matched_req = [t for t in require if t in text]
            if not matched_req:
                return RelevanceDecision(
                    accepted=False,
                    reason="financial excerpt lacks pathway financial capacity tokens",
                )
            return RelevanceDecision(
                accepted=True,
                reason="financial capacity tokens present",
                matched_identity=matched_req,
            )

    # Generic passport / who-can-help crumbs are not pathway documents evidence
    if field == "required_documents":
        generic_only = (
            "passport" in text
            and "who-can-help" not in text
            and len(text) < 80
            and not any(
                t in text
                for t in (profile.get("identity_tokens") or [])
            )
        )
        if text.count("passport") and "who can help" in text:
            return RelevanceDecision(
                accepted=False,
                reason="generic application-help page, not pathway documents list",
            )
        if generic_only and entity.normalized_id.startswith("nl_highly"):
            return RelevanceDecision(
                accepted=False,
                reason="unrelated passport reference without HSM document context",
            )

    identity = [t.lower() for t in (profile.get("identity_tokens") or [])]
    reject = [t.lower() for t in (profile.get("reject_tokens") or [])]
    matched_rej = [t for t in reject if t in text]
    matched_id = [t for t in identity if t in text]
    if matched_rej and not matched_id and field != "official_provenance":
        return RelevanceDecision(
            accepted=False,
            reason="excerpt rejects pathway context",
            matched_reject=matched_rej,
        )
    return RelevanceDecision(accepted=True, reason="excerpt accepted")


def filter_views_for_entity(
    views: list[ExtractionView],
    entity: NormalizedEntity,
    *,
    config: dict[str, Any] | None = None,
) -> tuple[list[ExtractionView], list[str]]:
    kept: list[ExtractionView] = []
    rejections: list[str] = []
    for view in views:
        decision = view_matches_pathway_identity(view, entity, config=config)
        if decision.accepted:
            kept.append(view)
        else:
            rejections.append(
                f"doc {view.document_id}: {decision.reason}"
            )
    return kept, rejections


def recalibrate_cell(
    cell: RequirementCell,
    entity: NormalizedEntity,
    views: list[ExtractionView],
    *,
    config: dict[str, Any] | None = None,
) -> tuple[RequirementCell, list[str]]:
    """Apply relevance filtering; demote COMPLETE/PARTIAL when evidence is unrelated."""
    notes: list[str] = list(cell.notes)
    questionable: list[str] = []

    if entity.entity_type != "pathway":
        # Hubs / unknowns are not readiness pathways
        if entity.entity_type == "unknown_slot":
            return (
                RequirementCell(
                    pathway_id=cell.pathway_id,
                    country=cell.country,
                    requirement_field=cell.requirement_field,
                    coverage_class=CoverageClass.UNEVALUATED,
                    evidence_refs=list(cell.evidence_refs),
                    needs_human_review=True,
                    notes=["unknown pathway slot — not assessed as pathway coverage"],
                    is_critical=cell.is_critical,
                    applicable=cell.applicable,
                ),
                [],
            )
        # information hub: preserve class but mark not for readiness
        notes.append("information_hub — excluded from pathway readiness")
        return (
            RequirementCell(
                pathway_id=cell.pathway_id,
                country=cell.country,
                requirement_field=cell.requirement_field,
                coverage_class=cell.coverage_class,
                evidence_refs=list(cell.evidence_refs),
                needs_human_review=cell.needs_human_review,
                notes=notes,
                is_critical=cell.is_critical,
                applicable=cell.applicable,
            ),
            [],
        )

    if not cell.applicable:
        return cell, []

    relevant_views, rejections = filter_views_for_entity(views, entity, config=config)
    questionable.extend(rejections)

    # Filter evidence refs by excerpt relevance
    kept_refs: list[EvidenceRef] = []
    for ref in cell.evidence_refs:
        dec = excerpt_relevant_for_field(
            ref.heading_or_excerpt,
            cell.requirement_field,
            entity,
            config=config,
        )
        if dec.accepted:
            # Also require the document to be among relevant views when views exist
            if relevant_views and ref.document_id not in {v.document_id for v in relevant_views}:
                questionable.append(
                    f"ref doc {ref.document_id} not in relevant view set"
                )
                continue
            kept_refs.append(ref)
        else:
            questionable.append(
                f"unrelated evidence rejected ({dec.reason}): {ref.heading_or_excerpt[:80]}"
            )

    new_class = cell.coverage_class
    needs_review = cell.needs_human_review

    if cell.coverage_class in {CoverageClass.COMPLETE, CoverageClass.PARTIAL}:
        if not kept_refs and cell.requirement_field != "official_provenance":
            new_class = CoverageClass.UNEVALUATED if rejections else CoverageClass.MISSING
            needs_review = True
            notes.append("6E.1: prior hits lacked pathway-relevant evidence")
        elif not kept_refs and cell.requirement_field == "official_provenance":
            if relevant_views:
                kept_refs = [
                    EvidenceRef(
                        document_id=v.document_id,
                        version_id=v.version_id,
                        extractor=v.extractor,
                        heading_or_excerpt=v.title or v.canonical_url,
                    )
                    for v in relevant_views[:5]
                ]
                if any(not v.is_thin for v in relevant_views):
                    new_class = CoverageClass.COMPLETE
                else:
                    new_class = CoverageClass.PARTIAL
            else:
                new_class = CoverageClass.UNEVALUATED
                needs_review = True
                notes.append("6E.1: no pathway-relevant official docs after filter")
        elif cell.coverage_class == CoverageClass.COMPLETE and len(kept_refs) < len(
            cell.evidence_refs
        ):
            # Some evidence dropped — require remaining strength
            if len(kept_refs) < 2 and cell.requirement_field != "official_provenance":
                new_class = CoverageClass.PARTIAL
                notes.append("6E.1: demoted COMPLETE→PARTIAL after relevance filter")

    # Ensure critical applicability flag
    critical = is_critical_field(
        cell.requirement_field,
        financial_applicable=entity.financial_applicable,
    )

    return (
        RequirementCell(
            pathway_id=cell.pathway_id,
            country=cell.country,
            requirement_field=cell.requirement_field,
            coverage_class=new_class,
            evidence_refs=kept_refs,
            needs_human_review=needs_review,
            notes=notes,
            is_critical=critical,
            applicable=cell.applicable,
        ),
        questionable,
    )


def is_critical_requirement(field: str) -> bool:
    return field in CRITICAL
