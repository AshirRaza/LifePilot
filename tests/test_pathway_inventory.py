"""Phase 6G systematic coverage expansion tests."""

from __future__ import annotations

import csv
from pathlib import Path

from lifepilot.coverage.normalization.normalize import load_normalization_config
from lifepilot.coverage.pathway_inventory.categories import CATEGORIES, JURISDICTIONS
from lifepilot.coverage.pathway_inventory.coverage import (
    build_country_coverage,
    category_unknown_counts,
)
from lifepilot.coverage.pathway_inventory.discovery import build_discovery_and_approval
from lifepilot.coverage.pathway_inventory.evidence import build_evidence_rows, evidence_summary
from lifepilot.coverage.pathway_inventory.inventory_data import build_static_inventory
from lifepilot.coverage.pathway_inventory.readiness import PREVIOUSLY_READY_6F2
from lifepilot.coverage.pathway_inventory.report import default_6g_paths, write_csv
from lifepilot.registry.urls import url_in_any_allowed_pattern

# Pathways known COMPLETE across all applicable critical fields in 6F.2 assessments.
_SEED_DOC_COUNTS = {
    "gb_skilled_worker": 5,
    "nl_naturalisation": 5,
    "nl_highly_skilled_migrant": 5,
    "sg_employment_pass": 5,
    "nl_permanent_residence": 3,
    "nl_student_residence": 2,
    "sg_permanent_resident": 2,
    "au_student_500": 2,
    "ie_critical_skills_employment_permit": 2,
    "ie_general_employment_permit": 2,
    "nz_citizenship_by_grant": 1,
    "ca_express_entry_hub": 10,
    "ca_federal_skilled_worker": 2,
    "ca_canadian_experience_class": 2,
    "ca_federal_skilled_trades": 2,
}


def _enriched_inventory() -> list[dict]:
    """Static inventory with corpus-like enrichment; no live DB required."""
    rows = []
    for raw in build_static_inventory():
        docs = _SEED_DOC_COUNTS.get(raw["pathway_id"], 0)
        approved = bool(raw.get("source_ids"))
        rows.append(
            {
                **raw,
                "approved_source_present": approved,
                "ingested_documents": docs,
                "last_successful_fetch": "",
                "last_successful_extraction": "",
                "source_approval_status": (
                    "approved_source_linked" if approved else "candidate_or_missing"
                ),
                "is_true_pathway": raw["entity_type"] == "true_pathway",
            }
        )
    return rows


def test_all_jurisdictions_and_categories_represented():
    rows = build_static_inventory()
    countries = {r["country"] for r in rows}
    assert set(JURISDICTIONS) <= countries
    for country in JURISDICTIONS:
        cats = {r["category"] for r in rows if r["country"] == country}
        if country == "EU":
            assert "long_term_residence" in cats or "short_term" in cats
        else:
            for cat in (
                "short_term",
                "education",
                "employment",
                "family",
                "long_term_residence",
                "citizenship",
            ):
                assert cat in cats, f"{country} missing {cat}"


def test_canada_express_entry_split_in_normalization_config():
    cfg = load_normalization_config()
    by_orig = {e["original_pathway_id"]: e for e in cfg["entities"]}
    assert by_orig["ca_express_entry"]["entity_type"] == "information_hub"
    assert by_orig["ca_express_entry"]["normalized_id"] == "ca_express_entry_hub"
    for pid in (
        "ca_federal_skilled_worker",
        "ca_canadian_experience_class",
        "ca_federal_skilled_trades",
    ):
        assert by_orig[pid]["entity_type"] == "pathway"


def test_inventory_distinguishes_hubs_and_true_pathways():
    rows = build_static_inventory()
    ee = next(r for r in rows if r["pathway_id"] == "ca_express_entry_hub")
    fsw = next(r for r in rows if r["pathway_id"] == "ca_federal_skilled_worker")
    assert ee["entity_type"] == "information_hub"
    assert fsw["entity_type"] == "true_pathway"
    assert fsw.get("parent_hub_id") == "ca_express_entry_hub"
    ie_hub = next(r for r in rows if r["pathway_id"] == "ie_employment_permits_hub")
    assert ie_hub["entity_type"] == "information_hub"
    assert any(
        r["pathway_id"] == "ie_critical_skills_employment_permit"
        and r["entity_type"] == "true_pathway"
        for r in rows
    )


def test_nz_citizenship_paths_not_merged():
    rows = build_static_inventory()
    ids = {r["pathway_id"] for r in rows}
    assert "nz_citizenship_by_grant" in ids
    assert "nz_citizenship_by_descent" in ids
    assert "nz_citizenship_by_birth" in ids


def test_nl_pr_vs_eu_long_term_distinguished():
    rows = build_static_inventory()
    assert any(r["pathway_id"] == "nl_permanent_residence" for r in rows)
    assert any(r["pathway_id"] == "nl_eu_long_term_resident" for r in rows)


def test_unknown_vs_not_offered():
    rows = build_static_inventory()
    assert all(r["entity_type"] != "not_offered" for r in rows)
    unknown = [r for r in rows if r["entity_type"] == "unknown_candidate"]
    assert unknown
    meta = category_unknown_counts(
        [{**r, "ingested_documents": 0, "approved_source_present": False} for r in rows]
    )
    assert meta["unknown_candidate_entities"] == len(unknown)


def test_evidence_does_not_inherit_hub_complete_to_ca_children():
    rows = build_evidence_rows(
        ica_doc_id=2252, inventory_rows=_enriched_inventory()
    )
    by = {(r["pathway_id"], r["requirement"]): r for r in rows}
    assert by[("ca_express_entry_hub", "eligibility")]["classification"] == "NOT_APPLICABLE"
    assert by[("ca_federal_skilled_worker", "eligibility")]["classification"] == "UNEVALUATED"
    assert by[("ca_canadian_experience_class", "financial_or_sponsorship")][
        "classification"
    ] == "NOT_APPLICABLE"


def test_coverage_denominators_are_explicit():
    inv = _enriched_inventory()
    ev = build_evidence_rows(ica_doc_id=2252, inventory_rows=inv)
    countries = build_country_coverage(inv, ev)
    assert len(countries) == len(JURISDICTIONS)
    for row in countries:
        assert row["inventory_denominator"] == "true_pathways_identified"
        assert "evidence_denominator" in row
        assert 0.0 <= row["inventory_coverage_rate"] <= 1.0
        assert 0.0 <= row["evidence_complete_rate"] <= 1.0
    summary = evidence_summary(ev)
    assert "applicable" in summary["denominator_definition"].lower()


def test_held_urls_not_in_allowlist_and_queued():
    _discovery, approval = build_discovery_and_approval()
    held = [
        "https://enterprise.gov.ie/en/publications/publication-files/critical-skills-employment-permits-checklist.pdf",
        "https://ind.nl/en/forms/6009.pdf",
    ]
    for url in held:
        row = next(a for a in approval if a.get("url") == url)
        assert row["within_allowlist"] is False
        assert row["approval_status"] == "pending_human_approval"
        assert not url_in_any_allowed_pattern(
            url,
            [
                "https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits*",
                "https://ind.nl/en/replace-extend-renew-and-change/permanent-residency/permanent-residence-permit*",
            ],
        )


def test_provisional_ready_set_includes_prior_when_complete():
    rows = build_evidence_rows(
        ica_doc_id=2252, inventory_rows=_enriched_inventory()
    )
    summary = evidence_summary(rows)
    ready = set(summary["provisionally_ready_pathways_from_matrix"])
    for pid in PREVIOUSLY_READY_6F2:
        assert pid in ready


def test_csv_writer_deterministic(tmp_path: Path):
    paths = default_6g_paths(tmp_path)
    rows = build_static_inventory()[:5]
    write_csv(paths["pathway_inventory"], rows)
    with paths["pathway_inventory"].open(encoding="utf-8") as fh:
        loaded = list(csv.DictReader(fh))
    assert len(loaded) == 5
    assert loaded[0]["pathway_id"]


def test_categories_constant_stable():
    assert "short_term" in CATEGORIES
    assert "employment" in CATEGORIES
    assert "citizenship" in CATEGORIES
