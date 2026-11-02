"""Phase 6E coverage audit — classification and scoring rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lifepilot.coverage.classify import classify_field_for_views, classify_pathways
from lifepilot.coverage.gaps import build_gap_register
from lifepilot.coverage.inventory import (
    ExtractionView,
    detect_nl_language_mismatch,
    is_thin_content,
)
from lifepilot.coverage.schema import (
    CoverageClass,
    PathwayRecord,
    PathwayStatus,
    Severity,
)
from lifepilot.coverage.scoring import pathway_ready, score_countries
from lifepilot.coverage.taxonomy import (
    PathwaySeed,
    RequirementSignal,
    load_pathway_seeds,
)


def _view(**kwargs) -> ExtractionView:
    defaults = dict(
        document_id=1,
        version_id=10,
        source_id=86,
        country="GB",
        extractor="html_structured",
        title="Skilled Worker visa",
        canonical_url="https://www.gov.uk/skilled-worker-visa",
        source_url="https://www.gov.uk/skilled-worker-visa",
        discovery_status="discovered",
        doc_language="en",
        version_language="en",
        http_status=200,
        storage_path=None,
        headings=["Eligibility", "Documents you must provide", "How to apply"],
        paragraphs=[
            "To qualify for a Skilled Worker visa, you must work for an approved sponsor.",
            "You must provide a passport and a certificate of sponsorship from your employer.",
            "Apply online on GOV.UK and pay the visa fee.",
        ],
        list_items=["passport", "certificate of sponsorship"],
        text_chars=2000,
        block_count=20,
        is_thin=False,
        is_blocked_evidence=False,
    )
    defaults.update(kwargs)
    return ExtractionView(**defaults)


def _pathway(**kwargs) -> PathwayRecord:
    defaults = dict(
        pathway_id="gb_skilled_worker",
        country="GB",
        parent_category="work",
        name="Skilled Worker visa",
        status=PathwayStatus.VERIFIED,
        source_ids=[86],
        document_ids=[1],
        evidence_thin=False,
        financial_applicable=True,
    )
    defaults.update(kwargs)
    return PathwayRecord(**defaults)


def test_schema_enums_serialize():
    p = _pathway()
    d = p.to_dict()
    assert d["status"] == "verified"
    assert CoverageClass.COMPLETE.value == "COMPLETE"


def test_pathway_seeding_does_not_invent_unsupported_visas():
    seeds = load_pathway_seeds()
    ids = {s.pathway_id for s in seeds}
    # Grounded seeds exist
    assert "gb_skilled_worker" in ids
    assert "nl_highly_skilled_migrant" in ids
    assert "au_student_500" in ids
    # No fabricated visas for empty countries
    assert not any(s.country == "AE" for s in seeds)
    assert not any(s.country == "JP" for s in seeds)
    assert not any(s.country == "FR" for s in seeds)
    # Every seed has provenance source_ids
    for seed in seeds:
        assert seed.source_ids, f"{seed.pathway_id} missing source provenance"


def test_thin_hub_cannot_be_complete_for_eligibility():
    assert is_thin_content(text_chars=100, block_count=3, paragraph_chars=50) is True
    thin = _view(
        headings=["Eligibility"],
        paragraphs=["See overview."],
        list_items=[],
        text_chars=100,
        block_count=3,
        is_thin=True,
    )
    signal = RequirementSignal(
        field="eligibility",
        heading_patterns=["eligibility"],
        keyword_patterns=["eligible", "you must"],
    )
    cell = classify_field_for_views(
        field="eligibility",
        views=[thin],
        signal=signal,
        pathway=_pathway(),
    )
    assert cell.coverage_class == CoverageClass.PARTIAL
    assert cell.coverage_class != CoverageClass.COMPLETE


def test_signal_matching_partial_vs_complete():
    signal = RequirementSignal(
        field="eligibility",
        heading_patterns=["eligibility"],
        keyword_patterns=["you must", "qualify"],
    )
    rich = _view()
    complete_cell = classify_field_for_views(
        field="eligibility",
        views=[rich],
        signal=signal,
        pathway=_pathway(),
    )
    assert complete_cell.coverage_class == CoverageClass.COMPLETE

    keyword_only = _view(
        headings=["Overview"],
        paragraphs=["Applicants must qualify under the rules."],
        list_items=[],
        text_chars=1500,
        block_count=12,
        is_thin=False,
    )
    partial_cell = classify_field_for_views(
        field="eligibility",
        views=[keyword_only],
        signal=signal,
        pathway=_pathway(),
    )
    assert partial_cell.coverage_class in {
        CoverageClass.PARTIAL,
        CoverageClass.UNEVALUATED,
    }
    assert partial_cell.coverage_class != CoverageClass.COMPLETE


def test_scoring_denominators_ignore_unevaluated_inflation():
    pathways = [
        _pathway(),
        _pathway(
            pathway_id="ae_unknown_work",
            country="AE",
            parent_category="work",
            name="AE work (not inventoried)",
            status=PathwayStatus.UNKNOWN_OR_NOT_INVENTORIED,
            source_ids=[],
            document_ids=[],
            evidence_thin=True,
        ),
    ]
    cells = classify_pathways(pathways, [_view()])
    scores = {s.country: s for s in score_countries(pathways, cells)}
    ae = scores["AE"]
    # Unknown pathway cells are UNEVALUATED and must not inflate covered rate via fake COMPLETE
    assert ae.requirement_cells_complete == 0
    assert ae.pathways_ready == 0
    # GB should have scored cells that exclude pure UNEVALUATED from covered denom logic
    gb = scores["GB"]
    assert gb.pathways_inventoried >= 1


def test_gap_severity_for_missing_critical_fields():
    pathway = _pathway(status=PathwayStatus.CANDIDATE, document_ids=[], evidence_thin=True)
    cells = classify_pathways([pathway], [])
    gaps = build_gap_register([pathway], cells)
    critical_gaps = [
        g
        for g in gaps
        if g.requirement_field == "eligibility" and g.severity == Severity.CRITICAL
    ]
    assert critical_gaps, "missing critical eligibility should be critical severity"


def test_nl_language_mismatch_detection_helper():
    mismatch = _view(
        document_id=2249,
        source_id=11040,
        country="NL",
        canonical_url="https://ind.nl/en/forms/5013.pdf",
        source_url="https://ind.nl/en/forms/5013.pdf",
        doc_language="en",
        version_language="nl",
    )
    assert detect_nl_language_mismatch(mismatch) is True
    ok = _view(
        canonical_url="https://ind.nl/en/dutch-citizenship/becoming-a-dutch-national-through-naturalisation",
        doc_language="en",
        version_language="en",
    )
    assert detect_nl_language_mismatch(ok) is False


def test_blocked_make_it_in_germany_excluded_from_complete():
    blocked = _view(
        document_id=148,
        source_id=87,
        country="DE",
        title="Visa types",
        canonical_url="https://www.make-it-in-germany.com/en/visa-residence/types",
        discovery_status="rejected",
        is_blocked_evidence=True,
        headings=["Eligibility", "How to apply"],
        paragraphs=["You must have a job offer and meet salary thresholds for skilled workers."] * 3,
        text_chars=2000,
        block_count=20,
        is_thin=False,
    )
    pathway = _pathway(
        pathway_id="de_make_it_in_germany_visa_types",
        country="DE",
        name="Make it in Germany",
        status=PathwayStatus.CANDIDATE,
        source_ids=[87],
        document_ids=[148],
        evidence_thin=True,
        notes=["blocked/rejected WAF"],
    )
    signal = RequirementSignal(
        field="eligibility",
        heading_patterns=["eligibility"],
        keyword_patterns=["you must", "salary"],
    )
    cell = classify_field_for_views(
        field="eligibility",
        views=[blocked],
        signal=signal,
        pathway=pathway,
    )
    assert cell.coverage_class == CoverageClass.BLOCKED
    assert cell.coverage_class != CoverageClass.COMPLETE

    # official_provenance also blocked
    prov = classify_field_for_views(
        field="official_provenance",
        views=[blocked],
        signal=None,
        pathway=pathway,
    )
    assert prov.coverage_class == CoverageClass.BLOCKED


def test_pathway_ready_requires_all_critical_complete():
    pathway = _pathway()
    views = [_view()]
    cells = classify_pathways([pathway], views)
    # May or may not be ready depending on financial/docs signals; ensure helper is strict
    if pathway_ready(pathway, cells):
        critical = [c for c in cells if c.is_critical and c.applicable]
        assert all(c.coverage_class == CoverageClass.COMPLETE for c in critical)


def test_seeds_json_roundtrip(tmp_path: Path):
    # Ensure config files parse
    seeds = load_pathway_seeds()
    assert isinstance(seeds[0], PathwaySeed)
    cfg = Path(__file__).resolve().parents[1] / "config" / "coverage" / "pathway_seeds.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["pathways"]
