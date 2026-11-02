"""Official-source discovery proposals and approval queue for Phase 6G."""

from __future__ import annotations

from typing import Any

from lifepilot.coverage.pathway_inventory.inventory_data import build_static_inventory


def build_discovery_and_approval() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (source_discovery_rows, approval_queue_rows)."""
    inventory = build_static_inventory()
    discovery: list[dict[str, Any]] = []
    approval: list[dict[str, Any]] = []

    # Known 6F.2 held proposals (explicit — do not fetch)
    held = [
        {
            "jurisdiction": "IE",
            "authority": "DETE",
            "pathway": "ie_critical_skills_employment_permit",
            "url": "https://enterprise.gov.ie/en/publications/publication-files/critical-skills-employment-permits-checklist.pdf",
            "discovery_origin": "phase_6f2_source_proposals / permit page href",
            "official_host_ok": True,
            "already_registered_source": True,
            "within_allowlist": False,
            "robots_status": "not_checked_this_phase",
            "expected_content_type": "pdf_checklist",
            "expected_requirements": "required_documents",
            "priority": 1,
            "approval_status": "pending_human_approval",
        },
        {
            "jurisdiction": "IE",
            "authority": "DETE",
            "pathway": "ie_general_employment_permit",
            "url": "https://enterprise.gov.ie/en/Publications/Publication-files/General-Employment-Permits-Checklist.pdf",
            "discovery_origin": "phase_6f2_source_proposals / permit page href",
            "official_host_ok": True,
            "already_registered_source": True,
            "within_allowlist": False,
            "robots_status": "not_checked_this_phase",
            "expected_content_type": "pdf_checklist",
            "expected_requirements": "required_documents",
            "priority": 1,
            "approval_status": "pending_human_approval",
        },
        {
            "jurisdiction": "IE",
            "authority": "DETE",
            "pathway": "ie_employment_permits_hub",
            "url": "https://enterprise.gov.ie/en/Publications/Employment-Permits-Checklists.html",
            "discovery_origin": "phase_6f2 / application-forms page",
            "official_host_ok": True,
            "already_registered_source": True,
            "within_allowlist": False,
            "robots_status": "not_checked_this_phase",
            "expected_content_type": "html_checklist_index",
            "expected_requirements": "required_documents",
            "priority": 2,
            "approval_status": "pending_human_approval",
        },
        {
            "jurisdiction": "NL",
            "authority": "IND",
            "pathway": "nl_permanent_residence",
            "url": "https://ind.nl/en/forms/6009.pdf",
            "discovery_origin": "phase_6f2 / PR page href",
            "official_host_ok": True,
            "already_registered_source": True,
            "within_allowlist": False,
            "robots_status": "not_checked_this_phase",
            "expected_content_type": "pdf_application_form",
            "expected_requirements": "required_documents",
            "priority": 1,
            "approval_status": "pending_human_approval",
        },
    ]
    for h in held:
        discovery.append(dict(h))
        approval.append(dict(h))

    # New authority candidates for countries without approved sources
    new_authorities = [
        {
            "jurisdiction": "AE",
            "authority": "UAE Official Portal / ICP",
            "pathway": "ae_work_residence",
            "url": "https://u.ae/en/information-and-services/visa-and-emirates-id",
            "discovery_origin": "global_candidates_6d4 / official portal",
            "official_host_ok": True,
            "already_registered_source": False,
            "within_allowlist": False,
            "robots_status": "not_checked_this_phase",
            "expected_content_type": "html_hub",
            "expected_requirements": "eligibility,application_process",
            "priority": 1,
            "approval_status": "pending_new_source_approval",
        },
        {
            "jurisdiction": "JP",
            "authority": "Immigration Services Agency",
            "pathway": "jp_engineer_specialist",
            "url": "https://www.moj.go.jp/isa/applications/status/engineer.html",
            "discovery_origin": "official ISA status pages (candidate)",
            "official_host_ok": True,
            "already_registered_source": False,
            "within_allowlist": False,
            "robots_status": "not_checked_this_phase",
            "expected_content_type": "html_pathway",
            "expected_requirements": "eligibility,required_documents,application_process",
            "priority": 1,
            "approval_status": "pending_new_source_approval",
        },
        {
            "jurisdiction": "FR",
            "authority": "Service-Public.fr / Interior",
            "pathway": "fr_passeport_talent",
            "url": "https://www.service-public.fr/particuliers/vosdroits/N19804",
            "discovery_origin": "global_candidates_6d4 fr_service_public",
            "official_host_ok": True,
            "already_registered_source": False,
            "within_allowlist": False,
            "robots_status": "not_checked_this_phase",
            "expected_content_type": "html_pathway",
            "expected_requirements": "eligibility,application_process",
            "priority": 1,
            "approval_status": "pending_new_source_approval",
        },
        {
            "jurisdiction": "GB",
            "authority": "UK Home Office",
            "pathway": "gb_student",
            "url": "https://www.gov.uk/student-visa",
            "discovery_origin": "official gov.uk (expand beyond skilled worker seed)",
            "official_host_ok": True,
            "already_registered_source": True,
            "within_allowlist": False,
            "robots_status": "not_checked_this_phase",
            "expected_content_type": "html_pathway",
            "expected_requirements": "eligibility,required_documents,application_process,financial_or_sponsorship",
            "priority": 2,
            "approval_status": "pending_allowlist_expansion",
        },
        {
            "jurisdiction": "DE",
            "authority": "BAMF",
            "pathway": "de_eu_blue_card",
            "url": "https://www.bamf.de/EN/Themen/MigrationAufenthalt/ZuwandererDrittstaaten/Migrathek/BlaueKarteEU/blauekarteeu-node.html",
            "discovery_origin": "BAMF official Blue Card guidance",
            "official_host_ok": True,
            "already_registered_source": True,
            "within_allowlist": False,
            "robots_status": "not_checked_this_phase",
            "expected_content_type": "html_pathway",
            "expected_requirements": "eligibility,required_documents,financial_or_sponsorship",
            "priority": 1,
            "approval_status": "pending_allowlist_expansion",
        },
        {
            "jurisdiction": "US",
            "authority": "USCIS",
            "pathway": "us_f1",
            "url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/students-and-employment",
            "discovery_origin": "USCIS students/employment pages under approved working hub scope check needed",
            "official_host_ok": True,
            "already_registered_source": True,
            "within_allowlist": False,
            "robots_status": "not_checked_this_phase",
            "expected_content_type": "html_pathway",
            "expected_requirements": "eligibility,application_process",
            "priority": 2,
            "approval_status": "pending_allowlist_or_in_scope_discovery",
        },
        {
            "jurisdiction": "AU",
            "authority": "Home Affairs",
            "pathway": "au_temporary_graduate_485",
            "url": "https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/temporary-graduate-485",
            "discovery_origin": "official visa listing sibling of student-500",
            "official_host_ok": True,
            "already_registered_source": False,
            "within_allowlist": False,
            "robots_status": "not_checked_this_phase",
            "expected_content_type": "html_pathway",
            "expected_requirements": "eligibility,required_documents,application_process",
            "priority": 1,
            "approval_status": "pending_new_source_approval",
        },
        {
            "jurisdiction": "NZ",
            "authority": "DIA",
            "pathway": "nz_citizenship_by_grant",
            "url": "https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/check-if-you-can-apply-for-nz-citizenship/",
            "discovery_origin": "need grant eligibility beyond self-check teaser",
            "official_host_ok": True,
            "already_registered_source": True,
            "within_allowlist": False,
            "robots_status": "not_checked_this_phase",
            "expected_content_type": "html_eligibility",
            "expected_requirements": "eligibility",
            "priority": 2,
            "approval_status": "pending_allowlist_expansion",
        },
    ]
    for row in new_authorities:
        discovery.append(row)
        approval.append(row)

    # Inventory-driven missing URL proposals for true pathways without sources
    for item in inventory:
        if item["entity_type"] != "true_pathway":
            continue
        if item.get("source_ids"):
            continue
        if not item.get("official_url"):
            discovery.append(
                {
                    "jurisdiction": item["country"],
                    "authority": item.get("authority") or "",
                    "pathway": item["pathway_id"],
                    "url": "",
                    "discovery_origin": "inventory_gap_no_url",
                    "official_host_ok": False,
                    "already_registered_source": False,
                    "within_allowlist": False,
                    "robots_status": "n/a",
                    "expected_content_type": "unknown",
                    "expected_requirements": "eligibility,required_documents,application_process",
                    "priority": 3,
                    "approval_status": "needs_url_research",
                }
            )
            continue
        discovery.append(
            {
                "jurisdiction": item["country"],
                "authority": item.get("authority") or "",
                "pathway": item["pathway_id"],
                "url": item["official_url"],
                "discovery_origin": "inventory_static_official_url",
                "official_host_ok": True,
                "already_registered_source": False,
                "within_allowlist": False,
                "robots_status": "not_checked_this_phase",
                "expected_content_type": "html_or_pdf",
                "expected_requirements": "eligibility,required_documents,application_process,financial_or_sponsorship",
                "priority": 2,
                "approval_status": "pending_new_source_or_allowlist",
            }
        )

    return discovery, approval
