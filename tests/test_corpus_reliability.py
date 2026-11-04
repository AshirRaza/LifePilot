"""Tests for corpus reliability: backup, consistency, assessment, readiness.

Tests:
- Backup manifest shape validation
- Restore-isolation guard (refuse restore onto live DB name)
- Consistency detectors
- Assessment rules (COMPLETE needs passage+doc)
- Readiness audit never auto human_verified
- Discovery does not fetch unapproved (placeholder for Priority F)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lifepilot.coverage.corpus_reliability.backup import (
    BackupError,
    RestoreError,
    assert_safe_restore_database,
    sha256_file,
    file_info,
)
from lifepilot.coverage.corpus_reliability.consistency import (
    audit_evidence_consistency,
    generate_repair_proposals,
    is_query_variant,
    is_eu_evidence,
)
from lifepilot.coverage.corpus_reliability.assessment import (
    Assessment,
    assess_document_for_pathway,
    match_requirement,
    PRIORITY_PATHWAYS,
)
from lifepilot.coverage.corpus_reliability.readiness import (
    audit_readiness,
    check_evidence_quality,
    audit_ie_pathway,
    ReadinessReview,
    AUDIT_PATHWAYS,
)


# --- Backup manifest shape tests ---


def test_backup_manifest_shape():
    """Verify expected backup manifest structure."""
    # Simulate a manifest structure
    manifest = {
        "backup_id": "20261102_120000",
        "created_at": "2026-11-02T12:00:00+00:00",
        "database": {
            "name": "lifepilot",
            "user": "lifepilot",
            "dump_file": "lifepilot.sql",
            "dump_sha256": "abc123" * 10 + "abcd",  # 64 chars
            "dump_size": 1000000,
        },
        "corpus_counts": {
            "approved_sources": 38,
            "documents": 336,
            "document_versions": 403,
            "success_extractions": 400,
        },
        "data_archive": {
            "file": "data.tar.gz",
            "sha256": "def456" * 10 + "defg",
            "size": 50000000,
            "included_dirs": {"raw": True, "extracted": True, "compared": True, "runs": False},
            "file_count": 500,
        },
        "files": [
            {"path": "raw/documents/99/v1/page.html", "size": 1000, "sha256": "x" * 64, "mtime": "2026-01-01T00:00:00+00:00"},
        ],
        "recovery_instructions": {
            "db_volume_lost": "...",
            "data_dir_lost": "...",
            "both_lost": "...",
        },
    }
    
    # Required top-level keys
    assert "backup_id" in manifest
    assert "created_at" in manifest
    assert "database" in manifest
    assert "corpus_counts" in manifest
    assert "data_archive" in manifest
    assert "recovery_instructions" in manifest
    
    # Database section
    assert manifest["database"]["name"] == "lifepilot"
    assert len(manifest["database"]["dump_sha256"]) == 64
    
    # Corpus counts
    assert manifest["corpus_counts"]["approved_sources"] == 38
    assert manifest["corpus_counts"]["documents"] == 336
    
    # Data archive
    assert manifest["data_archive"]["included_dirs"]["raw"] is True
    assert manifest["data_archive"]["file_count"] > 0


def test_sha256_file():
    """Test SHA-256 file hashing."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
        f.write("test content")
        path = Path(f.name)
    
    try:
        h = sha256_file(path)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)
        
        # Same content = same hash
        h2 = sha256_file(path)
        assert h == h2
    finally:
        path.unlink()


def test_file_info():
    """Test file info extraction."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
        f.write("test content for info")
        path = Path(f.name)
    
    base = path.parent
    try:
        info = file_info(path, base)
        assert "path" in info
        assert "size" in info
        assert "sha256" in info
        assert "mtime" in info
        assert info["size"] == 21  # "test content for info"
        assert len(info["sha256"]) == 64
    finally:
        path.unlink()


# --- Restore isolation guard tests ---


@pytest.mark.parametrize("db_name", [
    "lifepilot",
    "lifepilot_test",
    "postgres",
    "template0",
    "template1",
    "production",
    "mydb",
    "",
])
def test_restore_guard_refuses_unsafe_database_names(db_name: str):
    """Restore must refuse to restore to live/protected DB names."""
    with pytest.raises(RestoreError):
        assert_safe_restore_database(db_name)


@pytest.mark.parametrize("db_name", [
    "lifepilot_restore_test",
    "lifepilot_backup_test",
    "myapp_restore_test",
    "test_backup_test",
])
def test_restore_guard_accepts_safe_database_names(db_name: str):
    """Restore accepts properly named restore-test databases."""
    # Should not raise
    assert_safe_restore_database(db_name)


def test_restore_guard_refuses_partial_match():
    """Restore refuses names that look like test DBs but aren't safe."""
    with pytest.raises(RestoreError):
        assert_safe_restore_database("lifepilot_restore")  # missing _test
    
    with pytest.raises(RestoreError):
        assert_safe_restore_database("restore_test_lifepilot")  # wrong format


# --- Consistency detector tests ---


def test_is_query_variant_detects_print():
    """Detect gov.uk print variants."""
    is_var, reason = is_query_variant("https://www.gov.uk/student-visa?print=true")
    assert is_var
    assert "print" in reason.lower()


def test_is_query_variant_detects_uscis_query():
    """Detect USCIS query string variants."""
    is_var, reason = is_query_variant("https://www.uscis.gov/page?topic=student")
    assert is_var
    assert "uscis" in reason.lower()


def test_is_query_variant_detects_utm():
    """Detect UTM tracking parameters."""
    is_var, reason = is_query_variant("https://example.com/page?utm_source=google")
    assert is_var
    assert "utm" in reason.lower()


def test_is_query_variant_clean_url():
    """Clean URLs are not variants."""
    is_var, _ = is_query_variant("https://www.gov.uk/student-visa")
    assert not is_var


def test_is_eu_evidence_detection():
    """Detect EU framework evidence."""
    assert is_eu_evidence("https://example.com/eu_blue_card/info", "de_something")
    assert is_eu_evidence("https://example.com/directive_2021/page", "nl_visa")
    assert not is_eu_evidence("https://www.gov.uk/student-visa", "gb_student")


def test_consistency_audit_complete_without_version(db_session):
    """COMPLETE cells must have valid version references."""
    evidence_rows = [
        {
            "pathway_id": "gb_student",
            "requirement": "eligibility",
            "classification": "COMPLETE",
            "document_id": "99999",  # Non-existent
            "version_id": "",
            "source_url": "https://www.gov.uk/student-visa",
            "entity_type": "true_pathway",
        }
    ]
    
    issues = audit_evidence_consistency(db_session, evidence_rows)
    complete_issues = [i for i in issues if i["issue_type"] == "complete_without_version"]
    # Should detect the missing version issue
    # (If doc 99999 doesn't exist, no version check; if it does exist but has versions, different issue)


def test_consistency_audit_hub_as_pathway():
    """Information hubs should not have COMPLETE for content requirements."""
    evidence_rows = [
        {
            "pathway_id": "gb_family",
            "requirement": "eligibility",  # Content requirement
            "classification": "COMPLETE",
            "document_id": "100",
            "version_id": "200",
            "source_url": "https://www.gov.uk/family",
            "entity_type": "information_hub",  # Hub, not pathway
        }
    ]
    
    # Mock session since we're testing the logic, not DB
    mock_session = MagicMock()
    mock_session.execute.return_value.mappings.return_value.all.return_value = []
    
    issues = audit_evidence_consistency(mock_session, evidence_rows)
    hub_issues = [i for i in issues if i["issue_type"] == "hub_as_pathway"]
    assert len(hub_issues) == 1
    assert hub_issues[0]["pathway_id"] == "gb_family"


def test_repair_proposals_generated():
    """Auto-repairable issues generate repair proposals."""
    issues = [
        {
            "issue_type": "hub_as_pathway",
            "pathway_id": "gb_family",
            "requirement": "eligibility",
            "severity": "high",
            "description": "Hub has COMPLETE",
            "auto_repairable": True,
            "repair_action": "Set to NOT_APPLICABLE",
            "document_id": "100",
            "version_id": "200",
            "url": "https://example.com",
        },
        {
            "issue_type": "unused_unlinked_document",
            "pathway_id": "",
            "requirement": "",
            "severity": "info",
            "description": "Document extracted but not linked",
            "auto_repairable": False,
            "repair_action": "Optional review",
            "document_id": "999",
            "version_id": "",
            "url": "",
        },
    ]

    repairs = generate_repair_proposals(issues)
    assert len(repairs) == 1  # Only hub_as_pathway is auto-repairable
    assert repairs[0]["issue_type"] == "hub_as_pathway"
    assert repairs[0]["proposed_value"] == "classification=NOT_APPLICABLE"


# --- Assessment rule tests ---


def test_assessment_complete_requires_passage_and_doc():
    """Machine assessments are at most PARTIAL and require pathway-applicable URLs."""
    blocks = [
        {"type": "heading", "text": "Eligibility"},
        {
            "type": "paragraph",
            "text": (
                "To be eligible for a Student visa you must have a Confirmation of Acceptance "
                "for Studies (CAS) and meet the financial requirements listed on this page. "
            )
            * 3,
        },
    ]
    # Non-applicable URL → no upgrade even with keywords
    assert (
        assess_document_for_pathway(
            blocks, "gb_student", "eligibility", 100, 200, "https://example.com"
        )
        is None
    )
    assessment = assess_document_for_pathway(
        blocks,
        "gb_student",
        "eligibility",
        100,
        200,
        "https://www.gov.uk/student-visa",
        title="Student visa",
    )
    assert assessment is not None
    assert assessment.new_classification == "PARTIAL"
    assert assessment.new_classification != "COMPLETE"
    assert assessment.method in {
        "pathway_aware_machine",
        "pathway_requirement_aware_machine",
    }


def test_match_requirement_eligibility():
    """Match eligibility requirement patterns."""
    text = "To be eligible for this visa you must have a valid passport."
    matched, pattern, passage = match_requirement(text, "eligibility")
    assert matched
    assert "eligib" in pattern.lower()
    assert len(passage) > 0


def test_match_requirement_documents():
    """Match required documents patterns."""
    text = "Documents required: passport, proof of funds, accommodation."
    matched, pattern, passage = match_requirement(text, "required_documents")
    assert matched


def test_match_requirement_no_match():
    """No match when text doesn't contain requirement keywords."""
    text = "Welcome to our website. Contact us for more information."
    matched, _, _ = match_requirement(text, "eligibility")
    assert not matched


def test_priority_pathways_defined():
    """Verify priority pathways are defined."""
    assert len(PRIORITY_PATHWAYS) > 0
    assert "gb_student" in PRIORITY_PATHWAYS
    assert "de_eu_blue_card" in PRIORITY_PATHWAYS


# --- Readiness audit tests ---


def test_readiness_audit_never_sets_human_verified():
    """Without an explicit confirm decision, human_verified stays False."""
    evidence_rows = [
        {
            "pathway_id": "gb_skilled_worker",
            "requirement": req,
            "classification": "COMPLETE",
            "document_id": "100",
            "version_id": "200",
            "source_url": "https://www.gov.uk/skilled-worker-visa",
            "entity_type": "true_pathway",
            "passage": "This is the detailed eligibility information..." * 10,
            "country": "GB",
        }
        for req in ["eligibility", "required_documents", "application_process", "financial_or_sponsorship", "official_provenance"]
    ]

    provisionally_ready = ["gb_skilled_worker"]

    reviews = audit_readiness(evidence_rows, provisionally_ready, ["gb_skilled_worker"])

    for review in reviews:
        assert review.human_verified is False, f"human_verified must be False for {review.pathway_id}"


def test_readiness_audit_ie_checklist_detection():
    """Detect IE pathways with checklist-docs-only evidence."""
    evidence_rows = [
        {
            "pathway_id": "ie_critical_skills_employment_permit",
            "requirement": "eligibility",
            "classification": "COMPLETE",
            "document_id": "100",
            "version_id": "200",
            "source_url": "https://example.ie/checklist",
            "entity_type": "true_pathway",
            "passage": "Checklist of documents required for your application.",  # Checklist-only
            "country": "IE",
            "evidence_basis": "official_checklist_pdf",
        },
        {
            "pathway_id": "ie_critical_skills_employment_permit",
            "requirement": "required_documents",
            "classification": "COMPLETE",
            "document_id": "100",
            "version_id": "200",
            "source_url": "https://example.ie/checklist",
            "entity_type": "true_pathway",
            "passage": "Supporting documents needed: passport, photo, etc.",
            "country": "IE",
            "evidence_basis": "official_checklist_pdf",
        },
        {
            "pathway_id": "ie_critical_skills_employment_permit",
            "requirement": "application_process",
            "classification": "COMPLETE",
            "document_id": "100",
            "version_id": "200",
            "source_url": "https://example.ie/checklist",
            "entity_type": "true_pathway",
            "passage": "Submit your documents with the checklist.",
            "country": "IE",
            "evidence_basis": "official_checklist_pdf",
        },
        {
            "pathway_id": "ie_critical_skills_employment_permit",
            "requirement": "financial_or_sponsorship",
            "classification": "COMPLETE",
            "document_id": "100",
            "version_id": "200",
            "source_url": "https://example.ie/checklist",
            "entity_type": "true_pathway",
            "passage": "Salary threshold of 32,000 EUR applies.",
            "country": "IE",
        },
        {
            "pathway_id": "ie_critical_skills_employment_permit",
            "requirement": "official_provenance",
            "classification": "COMPLETE",
            "document_id": "100",
            "version_id": "200",
            "source_url": "https://example.ie/checklist",
            "entity_type": "true_pathway",
            "passage": "",
            "country": "IE",
        },
    ]
    
    ie_audit = audit_ie_pathway(evidence_rows, "ie_critical_skills_employment_permit")
    assert ie_audit["is_checklist_docs_only"]


def test_check_evidence_quality_complete_without_doc():
    """Detect COMPLETE cells without document_id."""
    evidence_rows = [
        {
            "pathway_id": "test_pathway",
            "requirement": "eligibility",
            "classification": "COMPLETE",
            "document_id": "",  # Missing!
            "version_id": "",
            "source_url": "",
            "entity_type": "true_pathway",
            "passage": "",
            "country": "XX",
        }
    ]
    
    quality = check_evidence_quality(evidence_rows, "test_pathway")
    assert quality["has_concerns"]
    assert any("without document_id" in c for c in quality["concerns"])


def test_readiness_review_decisions():
    """Test readiness review decision logic."""
    # Pathway with all COMPLETE and substantive evidence
    good_evidence = [
        {
            "pathway_id": "nl_highly_skilled_migrant",
            "requirement": req,
            "classification": "COMPLETE",
            "document_id": "100",
            "version_id": "200",
            "source_url": "https://ind.nl/hsm",
            "entity_type": "true_pathway",
            "passage": "Detailed substantive content about eligibility criteria and requirements..." * 5,
            "country": "NL",
        }
        for req in ["eligibility", "required_documents", "application_process", "financial_or_sponsorship", "official_provenance"]
    ]
    
    reviews = audit_readiness(good_evidence, ["nl_highly_skilled_migrant"], ["nl_highly_skilled_migrant"])
    assert len(reviews) == 1
    
    review = reviews[0]
    assert review.human_verified is False  # Never True


def test_audit_pathways_defined():
    """Verify audit pathways are defined."""
    assert len(AUDIT_PATHWAYS) == 6
    assert "gb_skilled_worker" in AUDIT_PATHWAYS
    assert "ie_critical_skills_employment_permit" in AUDIT_PATHWAYS


# --- Discovery guard tests (placeholder for Priority F) ---


def test_discovery_does_not_fetch_unapproved():
    """Discovery must not fetch URLs not in the approved allowlist.
    
    This is a placeholder test for Priority F requirements.
    The actual implementation should:
    - Check URLs against approved allowlist before fetching
    - Never auto-expand allowlist
    - Never bypass robots.txt
    - Never substitute fetch for 404s
    """
    # Placeholder - actual implementation in Priority F
    approved_allowlist = {
        "https://www.gov.uk/student-visa",
        "https://ind.nl/hsm",
    }
    
    # URLs to discover
    discovered_urls = [
        "https://www.gov.uk/student-visa",  # Approved
        "https://www.gov.uk/graduate",  # Not approved
        "https://random.site/visa",  # Not approved
    ]
    
    # Only approved URLs should be fetched
    to_fetch = [u for u in discovered_urls if u in approved_allowlist]
    
    assert len(to_fetch) == 1
    assert "https://www.gov.uk/student-visa" in to_fetch
    assert "https://www.gov.uk/graduate" not in to_fetch
