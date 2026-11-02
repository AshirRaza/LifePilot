"""Deterministic requirement classifiers over stored extractions."""

from __future__ import annotations

from lifepilot.coverage.inventory import ExtractionView
from lifepilot.coverage.schema import (
    CoverageClass,
    EvidenceRef,
    PathwayRecord,
    PathwayStatus,
    RequirementCell,
)
from lifepilot.coverage.taxonomy import (
    REQUIREMENT_FIELDS,
    RequirementSignal,
    field_applicable,
    is_critical_field,
    load_requirement_signals,
)

# COMPLETE needs a heading hit plus enough passage body (never thin hubs).
COMPLETE_MIN_HITS = 2
COMPLETE_MIN_EXCERPT_CHARS = 120
PARTIAL_MIN_HITS = 1


def _pattern_hit(text: str, patterns: list[str]) -> list[str]:
    hits: list[str] = []
    for pat in patterns:
        if pat and pat in text:
            hits.append(pat)
    return hits


def _collect_hits(
    view: ExtractionView,
    signal: RequirementSignal,
) -> tuple[list[EvidenceRef], int, int, bool]:
    """Return evidence refs, heading_hits, keyword_hits, uncertain."""
    refs: list[EvidenceRef] = []
    heading_hits = 0
    keyword_hits = 0
    uncertain = False

    for heading in view.headings:
        h = heading.lower()
        matched = _pattern_hit(h, signal.heading_patterns)
        if matched:
            heading_hits += 1
            refs.append(
                EvidenceRef(
                    document_id=view.document_id,
                    version_id=view.version_id,
                    extractor=view.extractor,
                    heading_or_excerpt=heading[:240],
                )
            )

    # Keyword scan over paragraphs + list items near content
    body_parts = list(view.paragraphs) + list(view.list_items)
    for part in body_parts:
        low = part.lower()
        matched = _pattern_hit(low, signal.keyword_patterns)
        if matched:
            keyword_hits += 1
            if len(refs) < 8:
                refs.append(
                    EvidenceRef(
                        document_id=view.document_id,
                        version_id=view.version_id,
                        extractor=view.extractor,
                        heading_or_excerpt=part[:240],
                    )
                )
        # Ambiguous short keyword-only crumbs
        if matched and len(part) < 40 and heading_hits == 0:
            uncertain = True

    return refs, heading_hits, keyword_hits, uncertain


def classify_field_for_views(
    *,
    field: str,
    views: list[ExtractionView],
    signal: RequirementSignal | None,
    pathway: PathwayRecord,
) -> RequirementCell:
    applicable = field_applicable(
        field, financial_applicable=pathway.financial_applicable
    )
    critical = is_critical_field(
        field, financial_applicable=pathway.financial_applicable
    )

    if not applicable:
        return RequirementCell(
            pathway_id=pathway.pathway_id,
            country=pathway.country,
            requirement_field=field,
            coverage_class=CoverageClass.NOT_APPLICABLE,
            is_critical=False,
            applicable=False,
            notes=["Field not applicable for this pathway"],
        )

    if pathway.status == PathwayStatus.UNKNOWN_OR_NOT_INVENTORIED:
        return RequirementCell(
            pathway_id=pathway.pathway_id,
            country=pathway.country,
            requirement_field=field,
            coverage_class=CoverageClass.UNEVALUATED,
            is_critical=critical,
            applicable=True,
            notes=["Parent category not inventoried"],
        )

    # official_provenance: grounded in approved official docs with usable extraction
    if field == "official_provenance":
        usable = [v for v in views if not v.is_blocked_evidence]
        blocked_only = bool(views) and not usable
        if blocked_only:
            return RequirementCell(
                pathway_id=pathway.pathway_id,
                country=pathway.country,
                requirement_field=field,
                coverage_class=CoverageClass.BLOCKED,
                is_critical=critical,
                applicable=True,
                notes=["Only blocked/rejected documents available"],
                evidence_refs=[
                    EvidenceRef(
                        document_id=v.document_id,
                        version_id=v.version_id,
                        extractor=v.extractor,
                        heading_or_excerpt=v.title or v.canonical_url,
                    )
                    for v in views[:3]
                ],
            )
        if usable:
            refs = [
                EvidenceRef(
                    document_id=v.document_id,
                    version_id=v.version_id,
                    extractor=v.extractor,
                    heading_or_excerpt=v.title or v.canonical_url,
                )
                for v in usable[:5]
            ]
            # Provenance COMPLETE when non-thin official extraction exists
            if any(not v.is_thin for v in usable):
                cls = CoverageClass.COMPLETE
            else:
                cls = CoverageClass.PARTIAL
            return RequirementCell(
                pathway_id=pathway.pathway_id,
                country=pathway.country,
                requirement_field=field,
                coverage_class=cls,
                evidence_refs=refs,
                is_critical=critical,
                applicable=True,
                notes=["Cited approved official source documents"],
            )
        if pathway.source_ids:
            return RequirementCell(
                pathway_id=pathway.pathway_id,
                country=pathway.country,
                requirement_field=field,
                coverage_class=CoverageClass.PARTIAL,
                is_critical=critical,
                applicable=True,
                notes=["Approved source named but no successful extraction yet"],
            )
        return RequirementCell(
            pathway_id=pathway.pathway_id,
            country=pathway.country,
            requirement_field=field,
            coverage_class=CoverageClass.MISSING,
            is_critical=critical,
            applicable=True,
        )

    if not views:
        if any(v.is_blocked_evidence for v in views):
            pass
        # No linked extractions
        blocked_sources = pathway.source_ids and set(pathway.source_ids) <= {87, 92}
        if blocked_sources or "blocked" in " ".join(pathway.notes).lower():
            return RequirementCell(
                pathway_id=pathway.pathway_id,
                country=pathway.country,
                requirement_field=field,
                coverage_class=CoverageClass.BLOCKED,
                is_critical=critical,
                applicable=True,
                notes=list(pathway.notes),
            )
        if pathway.status == PathwayStatus.CANDIDATE and pathway.source_ids:
            return RequirementCell(
                pathway_id=pathway.pathway_id,
                country=pathway.country,
                requirement_field=field,
                coverage_class=CoverageClass.MISSING,
                is_critical=critical,
                applicable=True,
                notes=["No linked extraction content to classify"],
            )
        return RequirementCell(
            pathway_id=pathway.pathway_id,
            country=pathway.country,
            requirement_field=field,
            coverage_class=CoverageClass.UNEVALUATED,
            is_critical=critical,
            applicable=True,
            needs_human_review=True,
            notes=["No evidence to evaluate"],
        )

    usable = [v for v in views if not v.is_blocked_evidence]
    if not usable:
        return RequirementCell(
            pathway_id=pathway.pathway_id,
            country=pathway.country,
            requirement_field=field,
            coverage_class=CoverageClass.BLOCKED,
            is_critical=critical,
            applicable=True,
            notes=["Blocked Make-it-in-Germany / rejected docs excluded from COMPLETE evidence"],
            evidence_refs=[
                EvidenceRef(
                    document_id=v.document_id,
                    version_id=v.version_id,
                    extractor=v.extractor,
                    heading_or_excerpt=v.title or v.canonical_url,
                )
                for v in views[:3]
            ],
        )

    if signal is None:
        return RequirementCell(
            pathway_id=pathway.pathway_id,
            country=pathway.country,
            requirement_field=field,
            coverage_class=CoverageClass.UNEVALUATED,
            is_critical=critical,
            applicable=True,
            needs_human_review=True,
            notes=["No signal patterns configured"],
        )

    all_refs: list[EvidenceRef] = []
    total_heading = 0
    total_keyword = 0
    any_uncertain = False
    any_non_thin = False
    for view in usable:
        refs, hh, kh, uncertain = _collect_hits(view, signal)
        all_refs.extend(refs)
        total_heading += hh
        total_keyword += kh
        any_uncertain = any_uncertain or uncertain
        if not view.is_thin:
            any_non_thin = True

    total_hits = total_heading + total_keyword
    notes: list[str] = []

    if total_hits == 0:
        return RequirementCell(
            pathway_id=pathway.pathway_id,
            country=pathway.country,
            requirement_field=field,
            coverage_class=CoverageClass.MISSING,
            is_critical=critical,
            applicable=True,
            notes=["No heading/keyword signal hits in linked extractions"],
        )

    # Cap at PARTIAL for thin hubs
    thin_only = all(v.is_thin for v in usable)
    if thin_only or not any_non_thin:
        notes.append("Thin/hub content caps coverage at PARTIAL")
        return RequirementCell(
            pathway_id=pathway.pathway_id,
            country=pathway.country,
            requirement_field=field,
            coverage_class=CoverageClass.PARTIAL,
            evidence_refs=all_refs[:8],
            is_critical=critical,
            applicable=True,
            notes=notes,
        )

    if any_uncertain and total_heading == 0:
        return RequirementCell(
            pathway_id=pathway.pathway_id,
            country=pathway.country,
            requirement_field=field,
            coverage_class=CoverageClass.UNEVALUATED,
            evidence_refs=all_refs[:8],
            needs_human_review=True,
            is_critical=critical,
            applicable=True,
            notes=["Uncertain keyword-only match; never guess COMPLETE"],
        )

    excerpt_chars = sum(len(r.heading_or_excerpt) for r in all_refs)
    if (
        total_heading >= 1
        and total_hits >= COMPLETE_MIN_HITS
        and excerpt_chars >= COMPLETE_MIN_EXCERPT_CHARS
        and any_non_thin
    ):
        cls = CoverageClass.COMPLETE
    elif total_hits >= PARTIAL_MIN_HITS:
        cls = CoverageClass.PARTIAL
        if total_heading == 0:
            notes.append("Keyword hits without heading match → PARTIAL")
    else:
        cls = CoverageClass.UNEVALUATED
        return RequirementCell(
            pathway_id=pathway.pathway_id,
            country=pathway.country,
            requirement_field=field,
            coverage_class=cls,
            evidence_refs=all_refs[:8],
            needs_human_review=True,
            is_critical=critical,
            applicable=True,
            notes=["Insufficient evidence for classification"],
        )

    return RequirementCell(
        pathway_id=pathway.pathway_id,
        country=pathway.country,
        requirement_field=field,
        coverage_class=cls,
        evidence_refs=all_refs[:8],
        is_critical=critical,
        applicable=True,
        notes=notes,
    )


def classify_pathways(
    pathways: list[PathwayRecord],
    extractions: list[ExtractionView],
    *,
    signals: dict[str, RequirementSignal] | None = None,
) -> list[RequirementCell]:
    sigs = signals if signals is not None else load_requirement_signals()
    by_doc: dict[int, ExtractionView] = {e.document_id: e for e in extractions}
    by_source: dict[int, list[ExtractionView]] = {}
    for e in extractions:
        by_source.setdefault(e.source_id, []).append(e)

    cells: list[RequirementCell] = []
    for pathway in pathways:
        views = [by_doc[d] for d in pathway.document_ids if d in by_doc]
        # If document_ids empty but sources present, do not silently use all source docs
        # for multi-pathway sources — inventory already linked docs.
        if not views and len(pathway.source_ids) == 1:
            # Candidate with no extractions yet
            views = []
        for field in REQUIREMENT_FIELDS:
            cells.append(
                classify_field_for_views(
                    field=field,
                    views=views,
                    signal=sigs.get(field),
                    pathway=pathway,
                )
            )
    return cells
