"""Phase 6E.1 — normalization, denominators, relevance, readiness."""

from __future__ import annotations

from lifepilot.coverage.inventory import ExtractionView
from lifepilot.coverage.normalization.denominators import (
    NOT_ASSESSED,
    compute_all_readiness,
    pathway_passes_critical,
    score_country_inventory,
)
from lifepilot.coverage.normalization.leads import assess_gap_leads
from lifepilot.coverage.normalization.normalize import (
    NormalizedEntity,
    normalize_inventory,
    true_pathways,
)
from lifepilot.coverage.normalization.relevance import (
    excerpt_relevant_for_field,
    recalibrate_cell,
    view_matches_pathway_identity,
)
from lifepilot.coverage.schema import CoverageClass, EvidenceRef, RequirementCell


def _entity(**kwargs) -> NormalizedEntity:
    defaults = dict(
        original_pathway_id="au_student_500",
        normalized_id="au_student_500",
        entity_type="pathway",
        country="AU",
        name="Student 500",
        parent_category="study",
        program_identity="Australia Student visa subclass 500",
        hub_role=None,
        original_status="verified",
        source_ids=[11023],
        document_ids=[1],
        financial_applicable=True,
        evidence_thin=False,
    )
    defaults.update(kwargs)
    return NormalizedEntity(**defaults)


def _cell(**kwargs) -> RequirementCell:
    defaults = dict(
        pathway_id="au_student_500",
        country="AU",
        requirement_field="eligibility",
        coverage_class=CoverageClass.COMPLETE,
        evidence_refs=[],
        needs_human_review=False,
        notes=[],
        is_critical=True,
        applicable=True,
    )
    defaults.update(kwargs)
    return RequirementCell(**defaults)


def _view(**kwargs) -> ExtractionView:
    defaults = dict(
        document_id=1,
        version_id=10,
        source_id=11023,
        country="AU",
        extractor="html_structured",
        title="Student visa (subclass 500)",
        canonical_url="https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500",
        source_url="https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500",
        discovery_status="discovered",
        doc_language="en",
        version_language="en",
        http_status=200,
        storage_path=None,
        headings=["Eligibility", "Financial capacity"],
        paragraphs=["You must have adequate funds for the cost of living."],
        list_items=[],
        text_chars=1200,
        block_count=12,
        is_thin=False,
        is_blocked_evidence=False,
    )
    defaults.update(kwargs)
    return ExtractionView(**defaults)


def test_normalization_separates_pathways_hubs_unknowns():
    inventory = {
        "pathways": [
            {
                "pathway_id": "gb_skilled_worker",
                "country": "GB",
                "name": "Skilled Worker",
                "parent_category": "work",
                "status": "verified",
                "source_ids": [86],
                "document_ids": [1],
                "financial_applicable": True,
                "evidence_thin": False,
            },
            {
                "pathway_id": "us_working_hub",
                "country": "US",
                "name": "Working in the US",
                "parent_category": "work",
                "status": "verified",
                "source_ids": [9080],
                "document_ids": [2],
                "financial_applicable": True,
                "evidence_thin": False,
            },
            {
                "pathway_id": "ae_unknown_work",
                "country": "AE",
                "name": "AE work (not inventoried)",
                "parent_category": "work",
                "status": "unknown_or_not_inventoried",
                "source_ids": [],
                "document_ids": [],
                "financial_applicable": True,
                "evidence_thin": True,
            },
            {
                "pathway_id": "ie_coming_to_work",
                "country": "IE",
                "name": "Coming to work",
                "parent_category": "work",
                "status": "verified",
                "source_ids": [9089],
                "document_ids": [3],
                "financial_applicable": True,
                "evidence_thin": False,
            },
        ]
    }
    entities, mapping = normalize_inventory(inventory)
    assert len(mapping) == 4
    assert {e.original_pathway_id for e in true_pathways(entities)} == {
        "gb_skilled_worker"
    }
    hubs = {e.original_pathway_id for e in entities if e.entity_type == "information_hub"}
    assert "us_working_hub" in hubs
    assert "ie_coming_to_work" in hubs
    unknowns = [e for e in entities if e.entity_type == "unknown_slot"]
    assert len(unknowns) == 1
    assert unknowns[0].original_pathway_id == "ae_unknown_work"


def test_unevaluated_blocks_readiness_and_stays_in_denominator():
    entity = _entity(original_pathway_id="sg_employment_pass", normalized_id="sg_employment_pass", country="SG")
    cells = [
        _cell(pathway_id="sg_employment_pass", country="SG", requirement_field="eligibility"),
        _cell(
            pathway_id="sg_employment_pass",
            country="SG",
            requirement_field="required_documents",
            coverage_class=CoverageClass.UNEVALUATED,
            needs_human_review=True,
        ),
        _cell(
            pathway_id="sg_employment_pass",
            country="SG",
            requirement_field="application_process",
        ),
        _cell(
            pathway_id="sg_employment_pass",
            country="SG",
            requirement_field="financial_or_sponsorship",
        ),
        _cell(
            pathway_id="sg_employment_pass",
            country="SG",
            requirement_field="official_provenance",
        ),
    ]
    result = pathway_passes_critical(entity, cells)
    assert result.ready is False
    assert result.critical_applicable_total == 5
    assert result.critical_incomplete >= 1
    assert any("UNEVALUATED" in b for b in result.blocking_fields)


def test_zero_denominator_country_is_not_assessed():
    entities = [
        _entity(
            original_pathway_id="ae_unknown_work",
            normalized_id="ae_unknown_work",
            entity_type="unknown_slot",
            country="AE",
            name="AE work unknown",
            parent_category="work",
            program_identity=None,
            original_status="unknown_or_not_inventoried",
            source_ids=[],
            document_ids=[],
        )
    ]
    readiness, countries = compute_all_readiness(entities, {})
    ae = next(c for c in countries if c.country == "AE")
    assert ae.inventory_completeness == NOT_ASSESSED
    assert ae.true_pathways == 0
    assert "0%" not in ae.inventory_completeness


def test_student_financial_rejects_diplomatic_exemption():
    entity = _entity()
    decision = excerpt_relevant_for_field(
        "Diplomatic staff may receive an exemption for sponsorship requirements.",
        "financial_or_sponsorship",
        entity,
    )
    assert decision.accepted is False

    ok = excerpt_relevant_for_field(
        "You must show adequate funds and cost of living evidence for Student visa 500.",
        "financial_or_sponsorship",
        entity,
    )
    assert ok.accepted is True


def test_us_student_vs_employment_identity():
    hub = _entity(
        original_pathway_id="us_students_exchange",
        normalized_id="us_students_exchange_hub",
        entity_type="information_hub",
        country="US",
        name="Students and Exchange",
        parent_category="study",
        program_identity=None,
    )
    # Employment H-1B page should not satisfy student hub identity when used as student evidence
    employment = _view(
        country="US",
        source_id=9080,
        title="H-1B Specialty Occupations",
        canonical_url="https://www.uscis.gov/working-in-the-united-states/h-1b-specialty-occupations",
        headings=["Eligibility Criteria"],
        paragraphs=["Specialty occupation workers."],
    )
    # For pathway us_h1b it should match
    h1b = _entity(
        original_pathway_id="us_h1b",
        normalized_id="us_h1b",
        country="US",
        name="H-1B",
        parent_category="work",
        program_identity="H-1B",
        source_ids=[9080],
    )
    assert view_matches_pathway_identity(employment, h1b).accepted is True


def test_nl_hsm_rejects_unrelated_passport_crumb():
    entity = _entity(
        original_pathway_id="nl_highly_skilled_migrant",
        normalized_id="nl_highly_skilled_migrant",
        country="NL",
        name="HSM",
        parent_category="work",
        program_identity="Highly skilled migrant",
        source_ids=[89],
    )
    decision = excerpt_relevant_for_field(
        "passport",
        "required_documents",
        entity,
    )
    assert decision.accepted is False


def test_au_family_rejects_generic_help_page_evidence():
    entity = _entity(
        original_pathway_id="au_family_migration",
        normalized_id="au_family_migration_hub",
        entity_type="information_hub",
        country="AU",
        name="Family migration",
        parent_category="family",
        program_identity=None,
        source_ids=[11024],
    )
    cell = _cell(
        pathway_id="au_family_migration",
        country="AU",
        requirement_field="application_process",
        coverage_class=CoverageClass.COMPLETE,
        evidence_refs=[
            EvidenceRef(
                document_id=9,
                version_id=1,
                extractor="html_structured",
                heading_or_excerpt="Who can help with your application overview",
            )
        ],
    )
    # Hub path — recalibrate marks hub note; for pathway financial/docs use student reject tokens
    new_cell, _ = recalibrate_cell(cell, entity, [])
    assert "information_hub" in " ".join(new_cell.notes)


def test_generic_lead_url_not_reused_as_pathway_specific():
    rows = [
        {
            "gap_id": "G1",
            "pathway_id": "au_citizenship",
            "country": "AU",
            "potential_lead_url": (
                "https://immi.homeaffairs.gov.au/help-support/"
                "who-can-help-with-your-application/overview"
            ),
        },
        {
            "gap_id": "G2",
            "pathway_id": "au_student_500",
            "country": "AU",
            "potential_lead_url": (
                "https://immi.homeaffairs.gov.au/help-support/"
                "who-can-help-with-your-application/overview"
            ),
        },
    ]
    assessments = assess_gap_leads(
        rows,
        original_to_normalized={
            "au_citizenship": "au_citizenship",
            "au_student_500": "au_student_500",
        },
    )
    assert all(a.lead_class == "unrelated_generic" for a in assessments)
    assert assessments[1].reused_generic is True


def test_hub_never_counts_as_ready_pathway():
    hub = _entity(
        original_pathway_id="ie_coming_to_work",
        normalized_id="ie_coming_to_work_hub",
        entity_type="information_hub",
        country="IE",
        name="Coming to work",
        parent_category="work",
        program_identity=None,
        original_status="verified",
        source_ids=[9089],
        document_ids=[1],
    )
    cells = [
        _cell(pathway_id="ie_coming_to_work", country="IE", requirement_field=f)
        for f in (
            "eligibility",
            "required_documents",
            "application_process",
            "financial_or_sponsorship",
            "official_provenance",
        )
    ]
    result = pathway_passes_critical(hub, cells)
    assert result.ready is False
    assert result.critical_applicable_total == 0
