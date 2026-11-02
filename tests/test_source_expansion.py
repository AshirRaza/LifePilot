"""Phase 6H tests: pure functions only (no live DB, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lifepilot.coverage.pathway_inventory.evidence import ALIAS
from lifepilot.coverage.pathway_inventory.inventory_data import build_static_inventory
from lifepilot.coverage.source_expansion.approve import (
    UnsafeScopeError,
    assert_narrow_patterns,
    is_narrow_pattern,
)
from lifepilot.coverage.source_expansion.canada import (
    PROGRAMS,
    assess_canada,
    program_differences,
)
from lifepilot.coverage.source_expansion.candidates import (
    PLAN_EXACT_ALLOWLIST,
    authorized_candidates,
    build_pending_approvals,
    dedupe_pending_rows,
)
from lifepilot.coverage.source_expansion.evidence import (
    build_evidence_6h,
    complete_without_evidence,
    evidence_summary_6h,
    has_evidence,
)
from lifepilot.coverage.source_expansion.metrics import (
    build_country_metrics,
    build_link_overlay,
    build_pathway_metrics,
    metrics_totals,
    pathway_document_matches,
    reconcile_entities,
)
from lifepilot.coverage.source_expansion.readiness import (
    assert_no_human_verified,
    build_readiness_6h,
)
from lifepilot.coverage.source_expansion.relevance import (
    ExtractionRecord,
    match_url_to_pathways,
)
from lifepilot.coverage.source_expansion.run import PRESERVED, fingerprint, run_phase_6h
from lifepilot.coverage.source_expansion.storage_audit import choose_sample, parse_compose
from lifepilot.coverage.source_expansion.validate import decide
from lifepilot.registry.urls import path_prefix_pattern

REPO = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------- helpers
def _inv(pid: str, country: str, entity: str = "true_pathway", **kw):
    return {
        "pathway_id": pid, "country": country, "entity_type": entity,
        "category": "employment", "official_url": kw.get("official_url"),
        "source_ids": kw.get("source_ids", []), "ingested_documents": kw.get("ingested", 0),
        "approved_source_present": False,
    }


def _rec(doc_id: int, url: str, blocks: list[dict]) -> ExtractionRecord:
    return ExtractionRecord(
        document_id=doc_id, version_id=doc_id * 10, version_number=1, title="t",
        source_url=url, blocks=blocks,
    )


def _p(text: str) -> dict:
    return {"type": "paragraph", "text": text}


def _h(text: str, level: int = 2) -> dict:
    return {"type": "heading", "text": text, "level": level}


def _l(*items: str) -> dict:
    return {"type": "list", "items": list(items)}


def _t(headers: list[str], rows: list[list[str]]) -> dict:
    return {"type": "table", "headers": headers, "rows": rows}


COMMON = [
    _p("You must be admissible to Canada."),
    _p("You plan to live outside the province of Quebec."),
    _p("You must take approved language tests in English or French and meet the minimum score in all 4 abilities."),
]


def _canada_records(*, drop: set[str] | None = None, drop_fsw_phrase: str | None = None):
    fsw_blocks = [
        _p("at least 1 year of continuous work or 1,560 hours in total"),
        _p("work experience must have been obtained within the last 10 years"),
        _p("in one or more of the TEER categories: 0, 1, 2, or 3"),
        _p("You need 67 points or higher"),
        _p("You need an Educational Credential Assessment for immigration purposes"),
        *COMMON,
        _p("You must show that you have enough money for you and your family to settle in Canada."),
        _p("When you submit a profile to the Express Entry pool."),
    ]
    if drop_fsw_phrase:
        fsw_blocks = [b for b in fsw_blocks if drop_fsw_phrase not in b["text"]]
    recs = {
        "fsw": _rec(1, "https://x/fsw", fsw_blocks),
        "cec": _rec(2, "https://x/cec", [
            _p("Your work experience must be gained by working in Canada"),
            _p("at least 1 year of work or 1,560 hours total"),
            _p("in the 3 years before you apply"),
            _p("There is no education requirement for the Canadian Experience Class."),
            *COMMON,
            _p("When you fill out your profile we ask where you plan to live"),
        ]),
        "fst": _rec(3, "https://x/fst", [
            _p("at least 2 years of full-time work experience (or 3,120 hours total) in a skilled trade"),
            _p("within the 5 years before you apply"),
            _p("occupations in Major Groups 72, 73, 82, 83, 92 and 93"),
            _l("a valid job offer of full-time employment for at least 1 year, or", "a certificate of qualification"),
            _p("There is no education requirement for the Federal Skilled Trades Program."),
            *COMMON,
            _p("You must show that you have enough money for you and your family"),
            _p("When you fill out your profile we ask where you plan to live"),
        ]),
        "language": _rec(4, "https://x/language", [
            _h("Federal Skilled Worker Program", 3), _h("First official language", 4),
            _t(["Language", "Minimum"], [["English", "CLB 7"]]),
            _h("Canadian Experience Class", 3),
            _t(["NOC TEER", "English"], [["TEER 0 or 1", "CLB 7"], ["TEER 2 or 3", "CLB 5"]]),
            _h("Federal Skilled Trades Program", 3),
            _t(["Language", "Abilities", "Min"],
               [["English", "Speaking and listening", "CLB 5"], ["English", "Reading and writing", "CLB 4"]]),
            _h("How long results are valid", 2),
        ]),
        "job_offer": _rec(5, "https://x/job", [
            _h("Other requirements by program", 2),
            _h("Federal Skilled Workers Program", 3),
            _p("If you don't have a valid job offer, you'll need to provide proof of funds to be approved."),
            _h("Federal Skilled Trades Program", 3),
            _p("If you don't have a valid job offer, you'll need a certificate of qualification in your skilled trade."),
            _h("Canadian Experience Class", 2),
            _p("You don't need a job offer to be eligible, but your employer may choose to give you one."),
        ]),
        "eca": _rec(6, "https://x/eca", [
            _p("you need an ECA to: be eligible as the principal applicant for Federal Skilled Workers Program"),
        ]),
        "pof": _rec(7, "https://x/pof", [
            _h("Who needs proof of funds", 2),
            _p("You need proof of funds to meet the minimum requirements of the:"),
            _l("Federal Skilled Worker Program", "Federal Skilled Trades Program"),
            _h("Who doesn't need proof of funds", 3),
            _p("You don't need to show enough money if:"),
            _l("you're applying under the Canadian Experience Class", "you're authorized to work in Canada and you have a valid job offer"),
            _h("How much money you need", 2),
            _t(["Number of family members", "Funds you need (CAD)"], [["1", "$15,263"]]),
            _p("You need a bank letter"),
        ]),
        "docs_hub": _rec(8, "https://x/docs", [
            _l("language test results", "proof of work experience", "police certificates",
               "proof of funds", "certificate of qualification in a trade occupation"),
        ]),
        "create_profile": _rec(9, "https://x/profile", [
            _p("your first step is to create your profile and submit it to the Express Entry pool."),
            _l("invite you to apply to be a permanent resident"),
        ]),
        "apply_pr": _rec(10, "https://x/apply", [
            _l("Fill out the application form", "Upload your documents", "Pay the fees", "Submit your application"),
            _p("Your invitation to apply is valid for 60 days only"),
        ]),
        "rounds": _rec(11, "https://x/rounds", [_p("rounds")]),
    }
    for k in drop or set():
        recs.pop(k, None)
    return recs


def _by(rows):
    return {(r["pathway_id"], r["requirement"]): r for r in rows}


# ----------------------------------------------------------------------------- reconciliation
def test_entity_totals_reconcile_to_149_including_cross_cutting():
    rows = build_static_inventory()
    rec = reconcile_entities(rows)
    c = rec["entity_type_counts"]
    assert (c["true_pathway"], c["information_hub"], c["cross_cutting_guidance"], c["unknown_candidate"]) == (
        102, 33, 7, 7,
    )
    assert rec["total_entities"] == rec["entity_type_sum"] == 149
    assert rec["reconciles"] and rec["matches_expected_149"]
    assert "102 + 33 + 7 + 7 = 149" == rec["formula"]
    assert sum(b["total"] for b in rec["by_country"].values()) == 149


def test_reconcile_flags_unknown_entity_type_instead_of_dropping_it():
    rows = [_inv("a", "GB"), _inv("b", "GB", "mystery")]
    rec = reconcile_entities(rows)
    assert rec["unrecognised_entity_types"] == {"mystery": 1}
    assert rec["entity_type_sum"] == rec["total_entities"] == 2


# ----------------------------------------------------------------------------- EU / source metrics
def _sources():
    return [
        {"id": 1, "name": "gb", "review_status": "approved", "country_code": "GB", "seed_url": "", "allowed_url_patterns": []},
        {"id": 91, "name": "eu", "review_status": "approved", "country_code": None, "seed_url": "", "allowed_url_patterns": []},
        {"id": 5, "name": "cand", "review_status": "candidate", "country_code": "DE", "seed_url": "", "allowed_url_patterns": []},
    ]


def test_eu_source_is_separate_from_national_totals_and_country_vs_pathway_link_differ():
    inv = [
        _inv("gb_a", "GB", source_ids=[1]),
        _inv("gb_b", "GB"),
        _inv("de_a", "DE", source_ids=[5]),  # candidate (not approved) source
        _inv("eu_hub", "EU", "information_hub", source_ids=[91]),
    ]
    pm = build_pathway_metrics(inv, approved_source_ids={1, 91}, overlay={}, doc_matches={})
    countries = build_country_metrics(inv, pm, _sources())
    by = {r["country"]: r for r in countries}
    assert by["GB"]["country_has_approved_source"] is True
    assert by["GB"]["pathway_source_linked"] == 1 and by["GB"]["true_pathways"] == 2
    assert by["DE"]["country_has_approved_source"] is False
    assert by["DE"]["pathway_source_linked"] == 0
    assert by["EU"]["scope"] == "eu_framework" and by["EU"]["country_has_approved_source"] is True
    assert by["EU"]["pathway_source_linked"] == 0  # hub is not a true pathway
    totals = metrics_totals(countries)
    assert totals["national"]["entities_total"] == 3  # EU hub excluded
    assert totals["eu_framework"]["entities_total"] == 1


def test_owner_decision_overlay_links_a_new_source_to_its_pathway_only():
    decisions = [
        {"decision": "approved", "source_id": 77, "pathway_ids": "gb_student", "candidate_id": "GB", "decision_code": "approved_new_source"},
        {"decision": "rejected", "source_id": "", "pathway_ids": "jp_x", "candidate_id": "JP", "decision_code": "rejected_not_found"},
    ]
    overlay = build_link_overlay(decisions, approved_source_ids={77})
    assert set(overlay) == {"gb_student"}
    inv = [_inv("gb_student", "GB"), _inv("gb_skilled_worker", "GB", source_ids=[86])]
    pm = {p["pathway_id"]: p for p in build_pathway_metrics(inv, approved_source_ids={77, 86}, overlay=overlay, doc_matches={})}
    assert pm["gb_student"]["pathway_source_linked"] and pm["gb_student"]["link_basis"] == "owner_decision_6h"
    assert pm["gb_skilled_worker"]["link_basis"] == "inventory_source_ids"


# ----------------------------------------------------------------------------- relevance
def test_pathway_relevance_uses_url_tokens_not_source_wide_counts():
    inv = [
        _inv("gb_student", "GB", official_url="https://www.gov.uk/student-visa"),
        _inv("gb_skilled_worker", "GB", official_url="https://www.gov.uk/skilled-worker-visa", ingested=40),
    ]
    docs = [
        {"document_id": 1, "canonical_url": "https://www.gov.uk/student-visa/money", "country": "GB", "extraction_status": "success"},
        {"document_id": 2, "canonical_url": "https://www.gov.uk/unrelated-hub", "country": "GB", "extraction_status": "success"},
    ]
    m = pathway_document_matches(docs, inv)
    assert m["gb_student"]["strong"] == [1]
    assert "gb_skilled_worker" not in m  # 40 source-wide docs do not make it relevant
    pm = {p["pathway_id"]: p for p in build_pathway_metrics(inv, approved_source_ids={86}, overlay={}, doc_matches=m)}
    assert pm["gb_student"]["pathway_relevant_document_ingested"] is True
    assert pm["gb_skilled_worker"]["pathway_relevant_document_ingested"] is False
    assert pm["gb_skilled_worker"]["legacy_source_wide_ingested_documents"] == 40


def test_country_guard_and_shared_pages_are_not_single_pathway_evidence():
    inv = [
        _inv("nl_permanent_residence", "NL"),
        _inv("nl_eu_long_term_resident", "NL"),
        _inv("gb_student", "GB"),
    ]
    shared = match_url_to_pathways("https://ind.nl/en/residence-permits/permanent-residence-permit", "NL", inv)
    assert {m["pathway_id"] for m in shared} == {"nl_permanent_residence", "nl_eu_long_term_resident"}
    assert {m["strength"] for m in shared} == {"shared"}
    assert match_url_to_pathways("https://www.gov.uk/student-visa", "NL", inv) == []


# ----------------------------------------------------------------------------- dedupe / scope
def test_pending_queue_dedupes_by_normalized_url_and_pathway():
    rows = [
        {"jurisdiction": "GB", "pathway": "gb_student", "url": "https://www.gov.uk/student-visa/", "approval_status": "pending_new_source_or_allowlist", "reason": "inv", "origin": "inventory_gap"},
        {"jurisdiction": "GB", "pathway": "gb_student", "url": "https://WWW.gov.uk/student-visa", "approval_status": "pending_new_source_approval", "reason": "specific", "origin": "phase_6g"},
        {"jurisdiction": "GB", "pathway": "gb_graduate", "url": "https://www.gov.uk/student-visa", "approval_status": "pending", "reason": "other", "origin": "x"},
    ]
    out = dedupe_pending_rows(rows)
    assert len(out) == 2
    gb_student = next(r for r in out if r["pathway"] == "gb_student")
    assert gb_student["approval_status"] == "pending_new_source_approval"
    assert "inventory_gap" in gb_student["merged_origins"] and "phase_6g" in gb_student["merged_origins"]


def test_no_wildcards_beyond_path_prefix():
    assert is_narrow_pattern("https://www.gov.uk/student-visa*")
    assert is_narrow_pattern("https://ind.nl/en/forms/6009.pdf")
    assert not is_narrow_pattern("https://www.gov.uk/*")
    assert not is_narrow_pattern("https://*.gov.uk/student-visa*")
    with pytest.raises(UnsafeScopeError):
        assert_narrow_patterns(["https://www.gov.uk/*"])
    for c in authorized_candidates():
        if c.new_source is not None:
            pat = path_prefix_pattern(c.new_source.seed_url)
            assert_narrow_patterns([pat])
            assert pat.count("*") <= 1 and (pat.endswith("*") or "*" not in pat)
        if c.plan == PLAN_EXACT_ALLOWLIST:
            assert "*" not in c.authorized_url


def test_authorized_candidates_match_owner_decisions():
    cands = {c.candidate_id: c for c in authorized_candidates()}
    assert len(cands) == 12
    assert cands["GB-student-visa"].new_source.authority_id == 68
    assert cands["GB-student-visa"].new_source.jurisdiction_id == 100
    assert cands["DE-bamf-eu-blue-card"].new_source.authority_id == 8168
    assert cands["AE-uae-portal-visa-emirates-id"].new_source.authority_id == 8171
    assert cands["AU-temporary-graduate-485"].new_source.authority_id == 6668
    assert cands["IE-critical-skills-checklist"].source_id == 9090
    assert cands["NL-ind-form-6009"].authorized_url == "https://ind.nl/en/forms/6009.pdf"
    assert cands["NZ-check-if-you-can-apply"].not_accessible_label == "rejected_broken"
    assert cands["JP-isa-engineer-specialist"].not_accessible_label == "rejected_not_found"


# ----------------------------------------------------------------------------- decisions
def _probe(**kw):
    base = {
        "robots": {"can_fetch": True, "summary": "ok"},
        "http_status": 200, "final_url": "", "final_host": "", "content_type": "text/html",
        "redirect_chain_hosts": [], "host_ok": True, "accessible": True,
        "relevance_keywords_hit": True, "snippet": "<title>Visa</title>", "checked_at": "t",
    }
    base.update(kw)
    return base


def test_decisions_reject_404_hub_and_broken_urls_without_substitution():
    c = {x.candidate_id: x for x in authorized_candidates()}
    jp = decide(
        c["JP-isa-engineer-specialist"],
        _probe(http_status=404, accessible=False, final_url=c["JP-isa-engineer-specialist"].authorized_url,
               final_host="www.moj.go.jp", redirect_chain_hosts=["www.moj.go.jp"]),
    )
    assert (jp.decision, jp.decision_code) == ("rejected", "rejected_not_found")
    assert "do_not_fetch" in jp.restrictions
    fr = decide(
        c["FR-service-public-n19804"],
        _probe(final_url="https://www.service-public.gouv.fr/particuliers/vosdroits/N19804",
               final_host="www.service-public.gouv.fr", redirect_chain_hosts=["www.service-public.gouv.fr"],
               relevance_keywords_hit=False, snippet="<title>\u00c9tranger - Europe | Service-Public</title>"),
    )
    assert fr.decision_code == "rejected_insufficient_pathway_relevance"
    nz = decide(
        c["NZ-check-if-you-can-apply"],
        _probe(http_status=404, accessible=False, final_url="https://www.govt.nz/x", final_host="www.govt.nz",
               redirect_chain_hosts=["www.govt.nz"]),
        source_patterns=["https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship*"],
    )
    assert nz.decision_code == "rejected_broken"


def test_de_blue_card_approved_when_robots_unknown_but_restricted():
    c = {x.candidate_id: x for x in authorized_candidates()}["DE-bamf-eu-blue-card"]
    d = decide(
        c,
        _probe(robots={"can_fetch": None, "summary": "robots missing"},
               final_url=c.authorized_url, final_host="www.bamf.de", redirect_chain_hosts=["www.bamf.de"]),
    )
    assert d.decision == "approved"
    assert any("robots_unconfirmed_at_validation" in r for r in d.restrictions)
    blocked = decide(
        c,
        _probe(robots={"can_fetch": False, "summary": "disallowed"},
               final_url=c.authorized_url, final_host="www.bamf.de", redirect_chain_hosts=["www.bamf.de"]),
    )
    assert blocked.decision_code == "rejected_robots_disallowed"


def test_pending_queue_never_invents_replacement_urls():
    decisions = [
        {"candidate_id": "JP-isa-engineer-specialist", "decision_code": "rejected_not_found", "authorized_url": "https://www.moj.go.jp/isa/applications/status/engineer.html", "final_url": "", "pathway_ids": "jp_engineer_specialist"},
        {"candidate_id": "FR-service-public-n19804", "decision_code": "rejected_insufficient_pathway_relevance", "authorized_url": "https://www.service-public.fr/particuliers/vosdroits/N19804", "final_url": "https://www.service-public.gouv.fr/particuliers/vosdroits/N19804", "pathway_ids": "fr_passeport_talent"},
    ]
    rows = build_pending_approvals(decisions=decisions, inventory=[], candidate_sources=[], prior_queue_rows=[])
    jp = [r for r in rows if r["jurisdiction"] == "JP"]
    assert jp and all(r["url"] == "" for r in jp)  # research request, not a fetch target
    fr_urls = {r["url"] for r in rows if r["jurisdiction"] == "FR" and r["url"]}
    assert fr_urls <= {"https://www.service-public.gouv.fr/particuliers/vosdroits/N19804"}


# ----------------------------------------------------------------------------- Canada independence
def test_fsw_cec_fst_assessed_independently():
    rows = _by(assess_canada(_canada_records(), assessed_at="t"))
    for pid in PROGRAMS:
        assert rows[(pid, "eligibility")]["classification"] == "COMPLETE"
        assert rows[(pid, "required_documents")]["classification"] == "PARTIAL"
        assert rows[(pid, "application_process")]["classification"] == "COMPLETE"
        assert rows[(pid, "official_provenance")]["classification"] == "COMPLETE"
    assert rows[("ca_federal_skilled_worker", "financial_or_sponsorship")]["classification"] == "COMPLETE"
    assert rows[("ca_canadian_experience_class", "financial_or_sponsorship")]["classification"] == "NOT_APPLICABLE"
    assert rows[("ca_federal_skilled_trades", "financial_or_sponsorship")]["classification"] == "COMPLETE"


def test_removing_one_program_page_does_not_change_the_other_programs():
    full = _by(assess_canada(_canada_records(), assessed_at="t"))
    no_cec = _by(assess_canada(_canada_records(drop={"cec"}), assessed_at="t"))
    assert no_cec[("ca_canadian_experience_class", "eligibility")]["classification"] == "MISSING"
    for pid in ("ca_federal_skilled_worker", "ca_federal_skilled_trades"):
        for req in ("eligibility", "required_documents", "application_process", "financial_or_sponsorship"):
            assert no_cec[(pid, req)]["classification"] == full[(pid, req)]["classification"]


def test_missing_fsw_subrequirement_downgrades_only_fsw():
    rows = _by(assess_canada(_canada_records(drop_fsw_phrase="67 points"), assessed_at="t"))
    fsw = rows[("ca_federal_skilled_worker", "eligibility")]
    assert fsw["classification"] == "PARTIAL" and "selection_points_67" in fsw["missing_subrequirements"]
    assert rows[("ca_canadian_experience_class", "eligibility")]["classification"] == "COMPLETE"
    assert rows[("ca_federal_skilled_trades", "eligibility")]["classification"] == "COMPLETE"


def test_program_specific_language_minimums_are_not_inherited():
    # Remove the FST language table only: FSW/CEC still complete, FST not.
    recs = _canada_records()
    recs["language"] = _rec(4, "https://x/language", [
        b for b in recs["language"].blocks
        if not (b.get("type") == "table" and "Abilities" in b.get("headers", []))
    ])
    rows = _by(assess_canada(recs, assessed_at="t"))
    assert rows[("ca_federal_skilled_trades", "eligibility")]["classification"] == "PARTIAL"
    assert "language_clb_minimum_fst" in rows[("ca_federal_skilled_trades", "eligibility")]["missing_subrequirements"]
    assert rows[("ca_federal_skilled_worker", "eligibility")]["classification"] == "COMPLETE"
    assert rows[("ca_canadian_experience_class", "eligibility")]["classification"] == "COMPLETE"


def test_canada_rows_cite_documents_and_are_provisional():
    rows = assess_canada(_canada_records(), assessed_at="t")
    assert len(rows) == 15
    for r in rows:
        assert r["reviewer_status"] == "automated_provisional"
        if r["classification"] == "COMPLETE":
            assert r["document_id"] != "" and r["version_id"] != "" and r["evidence_spans"] != "[]"
    diff = program_differences(rows)
    assert set(diff["facts"]) == set(PROGRAMS)


# ----------------------------------------------------------------------------- evidence rules
def _base(pid, country, req, cls, **kw):
    return {
        "pathway_id": pid, "country": country, "category": "employment",
        "entity_type": kw.get("entity", "true_pathway"), "requirement": req,
        "classification": cls, "document_id": kw.get("doc", ""), "version_id": kw.get("ver", ""),
        "source_url": "", "passage": "", "explanation": "", "evidence_repair": kw.get("repair", "coverage_gap"),
        "reviewer_status": "automated_provisional", "ingested_documents": 0, "approved_source_present": False,
    }


def _pm(pid, country="IE", *, strong=(), linked=(), shared=()):
    return {
        "pathway_id": pid, "country": country, "entity_type": "true_pathway",
        "pathway_source_linked": bool(linked), "linked_source_ids": list(linked),
        "pathway_strong_document_ids": list(strong), "pathway_shared_document_ids": list(shared),
        "pathway_extracted_document_ids": list(strong), "pathway_relevant_document_ingested": bool(strong),
        "pathway_relevant_document_extracted": bool(strong), "legacy_source_wide_ingested_documents": 0,
        "link_basis": "", "pathway_source_linked_": False,
    }


def test_complete_requires_evidence_and_is_downgraded_otherwise():
    base = [_base("gb_x", "GB", "eligibility", "COMPLETE")]  # no doc id: inherited complete
    rows, changes = build_evidence_6h(
        base_rows=base, pathway_metrics=[_pm("gb_x", "GB")], extractions={},
        current_version_by_doc={}, canada_rows=[], new_doc_ids=set(), blocked_pathways={}, assessed_at="t",
    )
    assert rows[0]["classification"] == "PARTIAL"
    assert not complete_without_evidence(rows)
    assert changes[0]["change"] == "downgraded"
    ok = {"requirement": "eligibility", "document_id": 5}
    assert has_evidence(ok)
    assert not has_evidence({"requirement": "eligibility", "document_id": ""})
    assert has_evidence({"requirement": "official_provenance", "document_id": "", "evidence_source_ids": [1]})


def test_auto_match_is_at_most_partial():
    heading_body = " ".join(["word"] * 60)
    rec = _rec(9, "https://www.gov.uk/student-visa/eligibility", [
        _h("Eligibility requirements"), _p(heading_body),
    ])
    base = [_base("gb_student", "GB", "eligibility", "MISSING")]
    rows, _ = build_evidence_6h(
        base_rows=base, pathway_metrics=[_pm("gb_student", "GB", strong=[9])],
        extractions={9: rec}, current_version_by_doc={9: 90}, canada_rows=[], new_doc_ids={9},
        blocked_pathways={}, assessed_at="t",
    )
    assert rows[0]["classification"] == "PARTIAL"
    assert rows[0]["evidence_repair"] == "phase_6h_auto_match_partial"


def test_ie_checklist_pdf_upgrades_required_documents_but_form_pdf_does_not():
    checklist = ExtractionRecord(
        document_id=20, version_id=200, version_number=1, title="Critical Skills Checklist",
        source_url="https://enterprise.gov.ie/x/critical-skills-employment-permits-checklist.pdf",
        content_kind="pdf",
        blocks=[_p("Checklist of documents required"),
                _l("passport copy", "employment contract", "qualification certificates", "registration proof", "fee receipt")],
    )
    form = ExtractionRecord(
        document_id=21, version_id=210, version_number=1, title="Application form",
        source_url="https://ind.nl/en/forms/6009.pdf", content_kind="pdf",
        blocks=[_p("Surname First name Date of birth Place of birth Nationality Signature Please complete")],
    )
    base = [
        _base("ie_critical_skills_employment_permit", "IE", "required_documents", "PARTIAL", doc=2169),
        _base("nl_permanent_residence", "NL", "required_documents", "MISSING"),
    ]
    rows, _ = build_evidence_6h(
        base_rows=base,
        pathway_metrics=[
            _pm("ie_critical_skills_employment_permit", strong=[20]),
            _pm("nl_permanent_residence", "NL", strong=[21]),
        ],
        extractions={20: checklist, 21: form}, current_version_by_doc={}, canada_rows=[],
        new_doc_ids={20, 21}, blocked_pathways={}, assessed_at="t",
    )
    by = {r["pathway_id"]: r for r in rows}
    assert by["ie_critical_skills_employment_permit"]["classification"] == "COMPLETE"
    assert by["ie_critical_skills_employment_permit"]["version_id"] == 200
    assert by["nl_permanent_residence"]["classification"] == "PARTIAL"  # form, not an itemised list


def test_application_form_with_embedded_document_lists_is_not_a_checklist():
    form = ExtractionRecord(
        document_id=30, version_id=300, version_number=1, title="Aanvraag",
        source_url="https://ind.nl/en/forms/6009.pdf", content_kind="pdf",
        blocks=[
            _p("Surname First name Date of birth Place of birth Nationality Signature"),
            _p("Submit the following supporting documents with your application"),
            _p("\u2022 employer statement"), _p("\u2022 proof of income"),
            _p("\u2022 integration certificate"), _p("\u2022 passport copy"),
        ],
    )
    rows, _ = build_evidence_6h(
        base_rows=[_base("nl_permanent_residence", "NL", "required_documents", "MISSING")],
        pathway_metrics=[_pm("nl_permanent_residence", "NL", strong=[30])],
        extractions={30: form}, current_version_by_doc={}, canada_rows=[], new_doc_ids={30},
        blocked_pathways={}, assessed_at="t",
    )
    assert rows[0]["classification"] == "PARTIAL"


def test_published_6g_classification_is_the_change_baseline():
    base = [_base("gb_x", "GB", "eligibility", "MISSING")]
    rows, changes = build_evidence_6h(
        base_rows=base, pathway_metrics=[_pm("gb_x", "GB")], extractions={},
        current_version_by_doc={}, canada_rows=[], new_doc_ids=set(), blocked_pathways={},
        assessed_at="t", published_6g={("gb_x", "eligibility"): "UNEVALUATED"},
    )
    assert rows[0]["classification_6g"] == "UNEVALUATED"
    assert changes[0]["change"] == "newly_assessed"


def test_pdf_bullet_paragraphs_count_as_checklist_items():
    from lifepilot.coverage.source_expansion.relevance import classify_pdf_kind

    blocks = [_p("Below is a checklist of the documentation required"),
              _p("o Copy of passport"), _p("o Copy of contract"), _p("o Photo"), _p("o Fee receipt")]
    pdf = classify_pdf_kind(blocks, url="https://x/permit-checklist.pdf", title="Checklist")
    assert pdf["lists_documents"] and pdf["kind"] == "document_checklist"


def test_hub_only_provenance_is_downgraded_and_linked_pathway_page_completes_it():
    base = [
        _base("ae_work_residence", "AE", "official_provenance", "MISSING", repair="inventory"),
        _base("gb_student", "GB", "official_provenance", "PARTIAL", repair="inventory"),
        _base("sg_x", "SG", "official_provenance", "COMPLETE", repair="inventory"),
    ]
    pms = [
        _pm("ae_work_residence", "AE", linked=[501]),
        _pm("gb_student", "GB", strong=[7], linked=[502]),
        _pm("sg_x", "SG", linked=[91]),
    ]
    rows, _ = build_evidence_6h(
        base_rows=base, pathway_metrics=pms, extractions={}, current_version_by_doc={},
        canada_rows=[], new_doc_ids=set(), blocked_pathways={}, assessed_at="t",
    )
    by = {r["pathway_id"]: r for r in rows}
    assert by["ae_work_residence"]["classification"] == "PARTIAL"
    assert by["gb_student"]["classification"] == "COMPLETE" and by["gb_student"]["evidence_source_ids"] == [502]
    assert by["sg_x"]["classification"] == "PARTIAL"
    assert by["sg_x"]["evidence_repair"] == "phase_6h_hub_only_provenance_downgrade"


def test_blocked_fetch_marks_unevidenced_cells_blocked():
    base = [_base("de_eu_blue_card", "DE", "eligibility", "UNEVALUATED")]
    rows, changes = build_evidence_6h(
        base_rows=base, pathway_metrics=[_pm("de_eu_blue_card", "DE", linked=[600])], extractions={},
        current_version_by_doc={}, canada_rows=[], new_doc_ids=set(),
        blocked_pathways={"de_eu_blue_card": "robots disallowed at fetch time"}, assessed_at="t",
    )
    assert rows[0]["classification"] == "BLOCKED"
    assert changes[0]["change"] == "newly_assessed"


# ----------------------------------------------------------------------------- readiness
def test_no_human_verified_status_and_readiness_is_provisional():
    rows = [dict(_base("p", "GB", "eligibility", "COMPLETE", doc=1), reviewer_status="human_verified")]
    with pytest.raises(RuntimeError):
        assert_no_human_verified(rows)
    good = [dict(_base("p", "GB", r, "COMPLETE", doc=1, ver=10)) for r in (
        "eligibility", "required_documents", "application_process", "financial_or_sponsorship")]
    good.append(dict(_base("p", "GB", "official_provenance", "COMPLETE", doc=1, ver=10)))
    summary = evidence_summary_6h(good)
    assert summary["provisionally_ready_pathways_from_matrix"] == ["p"]
    ready = build_readiness_6h(
        good, evidence_summary=summary, change_summary={}, entity_reconciliation=reconcile_entities(build_static_inventory()),
        metrics_totals={}, ingestion={"delta": {}}, canada_summary={}, storage={"integrity_ok": True},
        baseline={"expected_from_6g": {}}, decisions=[],
    )
    assert ready["human_verified_ready_pathways"] == []
    assert ready["readiness_is_automated_provisional_only"] is True
    assert ready["invariants"]["complete_requires_evidence"] is True


# ----------------------------------------------------------------------------- storage
def test_compose_audit_distinguishes_named_volume_from_host_data():
    info = parse_compose(REPO / "compose.yaml")
    assert info["present"]
    assert "postgres_data" in info["named_volumes_declared"]
    assert any(v.startswith("postgres_data:") for v in info["named_volume_mounts"])
    assert info["raw_data_dir_mounted_in_any_container"] is False


def test_storage_sample_is_deterministic_and_includes_all_pdfs():
    versions = [
        {"version_id": i, "storage_path": f"raw/documents/{i}/v1.html", "content_kind": "html_page"}
        for i in range(1, 400)
    ] + [{"version_id": 1000, "storage_path": "raw/documents/9/v1.pdf", "content_kind": "pdf"}]
    a = choose_sample(versions)
    b = choose_sample(list(reversed(versions)))
    assert [v["version_id"] for v in a] == [v["version_id"] for v in b]
    assert 1000 in {v["version_id"] for v in a}
    assert len(a) < len(versions)


# ----------------------------------------------------------------------------- history intact
def test_historical_artifacts_and_aliases_intact(tmp_path):
    assert ALIAS["ca_express_entry"] == "ca_express_entry_hub"
    ids = {r["pathway_id"] for r in build_static_inventory()}
    assert {"ca_express_entry_hub", "ca_federal_skilled_worker", "nl_permanent_residence", "nl_eu_long_term_resident"} <= ids
    assert all(n.startswith("phase_6") for n in PRESERVED)
    assert "phase_6g_report.md" in PRESERVED and "phase_6f2_report.md" in PRESERVED
    (tmp_path / "phase_6g_report.md").write_text("x", encoding="utf-8")
    assert fingerprint(tmp_path) == fingerprint(tmp_path)
    before = fingerprint(tmp_path)
    (tmp_path / "phase_6g_report.md").write_text("y", encoding="utf-8")
    assert fingerprint(tmp_path) != before


def test_run_refuses_to_invent_a_baseline_after_writes(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_phase_6h(runs_dir=tmp_path, skip_ingestion=True, skip_tests=True)
