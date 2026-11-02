"""Authoritative Phase 6G pathway inventory seeds (research-backed, not invented programs).

Each row is a major immigration pathway/hub known to exist or an explicit unknown slot.
entity_type: true_pathway | information_hub | cross_cutting_guidance | unknown_candidate
status_hint: known | unknown | not_yet_researched
"""

from __future__ import annotations

from typing import Any

# Fields: pathway_id, country, category, name, entity_type, authority,
# official_url (may be None), source_ids, aliases, notes, confidence
InventoryRow = dict[str, Any]


def _p(
    pathway_id: str,
    country: str,
    category: str,
    name: str,
    entity_type: str,
    *,
    authority: str = "",
    official_url: str | None = None,
    source_ids: list[int] | None = None,
    aliases: list[str] | None = None,
    notes: str = "",
    confidence: str = "medium",
    parent_hub_id: str | None = None,
) -> InventoryRow:
    return {
        "pathway_id": pathway_id,
        "country": country,
        "category": category,
        "official_program_name": name,
        "entity_type": entity_type,
        "authority": authority,
        "official_url": official_url,
        "source_ids": source_ids or [],
        "aliases": aliases or [],
        "notes": notes,
        "confidence": confidence,
        "parent_hub_id": parent_hub_id,
    }


def build_static_inventory() -> list[InventoryRow]:
    rows: list[InventoryRow] = []

    # -------- GB --------
    rows += [
        _p("gb_visitor", "GB", "short_term", "Standard Visitor visa", "true_pathway", authority="UK Home Office", official_url="https://www.gov.uk/standard-visitor", confidence="high", notes="Major visitor route; source not yet approved in corpus"),
        _p("gb_student", "GB", "education", "Student visa", "true_pathway", authority="UK Home Office", official_url="https://www.gov.uk/student-visa", confidence="high"),
        _p("gb_skilled_worker", "GB", "employment", "Skilled Worker visa", "true_pathway", authority="UK Home Office", official_url="https://www.gov.uk/skilled-worker-visa", source_ids=[86], confidence="high"),
        _p("gb_global_talent", "GB", "employment", "Global Talent visa", "true_pathway", authority="UK Home Office", official_url="https://www.gov.uk/global-talent", confidence="high"),
        _p("gb_graduate", "GB", "education", "Graduate visa (post-study)", "true_pathway", authority="UK Home Office", official_url="https://www.gov.uk/graduate-visa", confidence="high"),
        _p("gb_innovator_founder", "GB", "business", "Innovator Founder visa", "true_pathway", authority="UK Home Office", official_url="https://www.gov.uk/innovator-founder-visa", confidence="high"),
        _p("gb_family", "GB", "family", "Family visas (partner/parent/child)", "information_hub", authority="UK Home Office", official_url="https://www.gov.uk/uk-family-visa", confidence="medium"),
        _p("gb_settlement_ilr", "GB", "long_term_residence", "Settle in the UK / Indefinite leave to remain", "true_pathway", authority="UK Home Office", official_url="https://www.gov.uk/settle-in-the-uk", confidence="high"),
        _p("gb_citizenship", "GB", "citizenship", "British citizenship (naturalisation)", "true_pathway", authority="UK Home Office", official_url="https://www.gov.uk/becoming-a-british-citizen", confidence="high"),
        _p("gb_fees_crosscut", "GB", "cross_cutting", "Visa fees", "cross_cutting_guidance", authority="UK Home Office", official_url="https://www.gov.uk/visa-fees", confidence="medium"),
    ]

    # -------- DE --------
    rows += [
        _p("de_schengen_visitor", "DE", "short_term", "Schengen visitor / short-stay visa", "true_pathway", authority="Federal Foreign Office", confidence="medium"),
        _p("de_student", "DE", "education", "Student residence permit", "true_pathway", authority="BAMF / AA", confidence="medium"),
        _p("de_eu_blue_card", "DE", "employment", "EU Blue Card (Germany)", "true_pathway", authority="BAMF", confidence="high", notes="Major skilled pathway; needs dedicated approved source"),
        _p("de_skilled_workers", "DE", "employment", "Skilled workers immigration Act routes", "information_hub", authority="BAMF", source_ids=[11034], confidence="medium"),
        _p("de_job_seeker", "DE", "employment", "Job-seeker / opportunity card candidates", "unknown_candidate", authority="BAMF", confidence="low"),
        _p("de_self_employment", "DE", "business", "Self-employment / freelance residence", "unknown_candidate", authority="BAMF", confidence="low"),
        _p("de_family_reunification", "DE", "family", "Family reunification (third-country)", "true_pathway", authority="BAMF", source_ids=[11035], confidence="high"),
        _p("de_settlement_permit", "DE", "long_term_residence", "Settlement permit (Niederlassungserlaubnis)", "true_pathway", authority="BAMF", confidence="high"),
        _p("de_naturalisation", "DE", "citizenship", "Naturalisation", "true_pathway", authority="BAMF", confidence="medium"),
        _p("de_aa_visas_hub", "DE", "cross_cutting", "Federal Foreign Office visa service hub", "information_hub", authority="AA", source_ids=[11036], confidence="medium"),
        _p("de_third_country_hub", "DE", "cross_cutting", "BAMF third-country immigrants hub", "information_hub", authority="BAMF", source_ids=[11033], confidence="medium"),
        _p("de_miig_blocked", "DE", "employment", "Make it in Germany visa types (blocked)", "information_hub", authority="Make it in Germany", source_ids=[87], confidence="low", notes="WAF/bot protection blocked"),
    ]

    # -------- NL --------
    rows += [
        _p("nl_short_stay", "NL", "short_term", "Short-stay Schengen visa", "true_pathway", authority="IND", confidence="medium"),
        _p("nl_student_residence", "NL", "education", "Student residence permit", "true_pathway", authority="IND", source_ids=[11038], confidence="high"),
        _p("nl_highly_skilled_migrant", "NL", "employment", "Highly skilled migrant", "true_pathway", authority="IND", source_ids=[89], confidence="high"),
        _p("nl_eu_blue_card", "NL", "employment", "EU Blue Card (Netherlands)", "true_pathway", authority="IND", confidence="medium"),
        _p("nl_orientation_year", "NL", "education", "Orientation year for highly educated persons", "true_pathway", authority="IND", confidence="medium"),
        _p("nl_start_up", "NL", "business", "Start-up residence permit", "true_pathway", authority="IND", confidence="medium"),
        _p("nl_self_employed", "NL", "business", "Self-employed person", "true_pathway", authority="IND", confidence="medium"),
        _p("nl_family_partner", "NL", "family", "Family and partner residence", "true_pathway", authority="IND", source_ids=[11037], confidence="high"),
        _p("nl_permanent_residence", "NL", "long_term_residence", "Permanent residence permit (national)", "true_pathway", authority="IND", source_ids=[11039], confidence="high", notes="Distinct from EU long-term resident"),
        _p("nl_eu_long_term_resident", "NL", "long_term_residence", "Long-term EU resident", "true_pathway", authority="IND", source_ids=[11039], confidence="high", notes="Assessed separately from national PR"),
        _p("nl_naturalisation", "NL", "citizenship", "Naturalisation", "true_pathway", authority="IND", source_ids=[11040], confidence="high"),
        _p("nl_work_permits_hub", "NL", "employment", "IND work residence permits hub", "information_hub", authority="IND", source_ids=[88], confidence="high"),
        _p("nl_fees_hub", "NL", "cross_cutting", "IND fees", "cross_cutting_guidance", authority="IND", source_ids=[11041], confidence="high"),
    ]

    # -------- SG --------
    rows += [
        _p("sg_visit", "SG", "short_term", "Visit / short-term stay", "information_hub", authority="ICA", confidence="medium"),
        _p("sg_student_pass", "SG", "education", "Student's Pass", "true_pathway", authority="ICA", confidence="medium"),
        _p("sg_employment_pass", "SG", "employment", "Employment Pass", "true_pathway", authority="MOM", source_ids=[9078], confidence="high"),
        _p("sg_s_pass", "SG", "employment", "S Pass", "true_pathway", authority="MOM", confidence="high"),
        _p("sg_entrepass", "SG", "business", "EntrePass", "true_pathway", authority="MOM", confidence="medium"),
        _p("sg_dependent_pass", "SG", "family", "Dependant's Pass", "true_pathway", authority="MOM", confidence="medium"),
        _p("sg_permanent_resident", "SG", "long_term_residence", "Permanent Resident", "true_pathway", authority="ICA", source_ids=[9079], confidence="high"),
        _p("sg_citizenship", "SG", "citizenship", "Singapore citizenship", "true_pathway", authority="ICA", confidence="medium"),
    ]

    # -------- US --------
    rows += [
        _p("us_b1_b2", "US", "short_term", "B-1/B-2 visitor", "true_pathway", authority="DOS/USCIS", confidence="medium"),
        _p("us_f1", "US", "education", "F-1 Student", "true_pathway", authority="USCIS/SEVP", confidence="high"),
        _p("us_j1", "US", "education", "J-1 Exchange Visitor", "true_pathway", authority="DOS", confidence="medium"),
        _p("us_h1b", "US", "employment", "H-1B Specialty Occupations", "true_pathway", authority="USCIS", source_ids=[9080], confidence="high"),
        _p("us_l1a", "US", "employment", "L-1A Intracompany Transferee", "true_pathway", authority="USCIS", source_ids=[9080], confidence="high"),
        _p("us_l1b", "US", "employment", "L-1B Specialized Knowledge", "true_pathway", authority="USCIS", source_ids=[9080], confidence="high"),
        _p("us_o1", "US", "employment", "O-1 Extraordinary Ability", "true_pathway", authority="USCIS", source_ids=[9080], confidence="high"),
        _p("us_e2", "US", "business", "E-2 Treaty Investors", "true_pathway", authority="USCIS", source_ids=[9080], confidence="high"),
        _p("us_e3", "US", "employment", "E-3 Specialty Occupation (Australia)", "true_pathway", authority="USCIS", source_ids=[9080], confidence="high"),
        _p("us_family_preference", "US", "family", "Family-based immigrant visas", "information_hub", authority="USCIS/DOS", confidence="medium"),
        _p("us_eb1", "US", "long_term_residence", "EB-1 First Preference", "true_pathway", authority="USCIS", source_ids=[9080], confidence="high"),
        _p("us_eb2", "US", "long_term_residence", "EB-2 Second Preference", "true_pathway", authority="USCIS", source_ids=[9080], confidence="high"),
        _p("us_eb3", "US", "long_term_residence", "EB-3 Third Preference", "true_pathway", authority="USCIS", source_ids=[9080], confidence="high"),
        _p("us_eb5", "US", "business", "EB-5 Immigrant Investor", "true_pathway", authority="USCIS", source_ids=[9080], confidence="high"),
        _p("us_naturalisation", "US", "citizenship", "Naturalization", "true_pathway", authority="USCIS", confidence="high"),
        _p("us_working_hub", "US", "employment", "USCIS Working in the United States hub", "information_hub", authority="USCIS", source_ids=[9080], confidence="high"),
        _p("us_students_exchange_hub", "US", "education", "Students and Exchange Visitors hub", "information_hub", authority="USCIS", source_ids=[9080], confidence="high"),
    ]

    # -------- AU --------
    rows += [
        _p("au_visitor_600", "AU", "short_term", "Visitor visa (subclass 600)", "true_pathway", authority="Home Affairs", confidence="high"),
        _p("au_eta_601", "AU", "short_term", "Electronic Travel Authority (subclass 601)", "true_pathway", authority="Home Affairs", confidence="medium"),
        _p("au_student_500", "AU", "education", "Student visa (subclass 500)", "true_pathway", authority="Home Affairs", source_ids=[11023], confidence="high"),
        _p("au_temporary_graduate_485", "AU", "education", "Temporary Graduate visa (subclass 485)", "true_pathway", authority="Home Affairs", confidence="high"),
        _p("au_skills_in_demand_482", "AU", "employment", "Skills in Demand visa (subclass 482)", "true_pathway", authority="Home Affairs", source_ids=[11027], confidence="high"),
        _p("au_skilled_independent_189", "AU", "employment", "Skilled Independent visa (subclass 189)", "true_pathway", authority="Home Affairs", confidence="high"),
        _p("au_skilled_nominated_190", "AU", "employment", "Skilled Nominated visa (subclass 190)", "true_pathway", authority="Home Affairs", confidence="high"),
        _p("au_business_innovation", "AU", "business", "Business Innovation and Investment visas", "information_hub", authority="Home Affairs", confidence="medium"),
        _p("au_partner_visa", "AU", "family", "Partner visas", "information_hub", authority="Home Affairs", source_ids=[11024], confidence="medium"),
        _p("au_permanent_residence_hub", "AU", "long_term_residence", "Permanent residence pathways hub", "information_hub", authority="Home Affairs", confidence="low"),
        _p("au_citizenship", "AU", "citizenship", "Australian citizenship", "true_pathway", authority="Home Affairs", source_ids=[11025], confidence="high"),
        _p("au_skilled_migration_hub", "AU", "employment", "Skilled migration program hub", "information_hub", authority="Home Affairs", source_ids=[9082], confidence="high"),
        _p("au_working_in_australia_hub", "AU", "employment", "Working in Australia hub", "information_hub", authority="Home Affairs", source_ids=[11026], confidence="high"),
        _p("au_family_migration_hub", "AU", "family", "Family migration program hub", "information_hub", authority="Home Affairs", source_ids=[11024], confidence="high"),
    ]

    # -------- CA --------
    rows += [
        _p("ca_visitor_eta", "CA", "short_term", "Visitor visa / eTA", "information_hub", authority="IRCC", confidence="medium"),
        _p("ca_study_permit", "CA", "education", "Study permit", "true_pathway", authority="IRCC", confidence="high"),
        _p("ca_work_permit_hub", "CA", "employment", "Work permits hub", "information_hub", authority="IRCC", confidence="medium"),
        _p("ca_express_entry_hub", "CA", "long_term_residence", "Express Entry (system hub)", "information_hub", authority="IRCC", source_ids=[9083], aliases=["ca_express_entry"], confidence="high", notes="Manages FSW/CEC/FST; not itself a single eligibility pathway"),
        _p("ca_federal_skilled_worker", "CA", "long_term_residence", "Federal Skilled Worker Program", "true_pathway", authority="IRCC", source_ids=[9083], parent_hub_id="ca_express_entry_hub", official_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry/who-can-apply/federal-skilled-workers.html", confidence="high"),
        _p("ca_canadian_experience_class", "CA", "long_term_residence", "Canadian Experience Class", "true_pathway", authority="IRCC", source_ids=[9083], parent_hub_id="ca_express_entry_hub", official_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry/who-can-apply/canadian-experience-class.html", confidence="high"),
        _p("ca_federal_skilled_trades", "CA", "long_term_residence", "Federal Skilled Trades Program", "true_pathway", authority="IRCC", source_ids=[9083], parent_hub_id="ca_express_entry_hub", official_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry/who-can-apply/federal-skilled-trades.html", confidence="high"),
        _p("ca_pnp", "CA", "long_term_residence", "Provincial Nominee Program", "information_hub", authority="IRCC/provinces", confidence="medium"),
        _p("ca_start_up_visa", "CA", "business", "Start-up Visa", "true_pathway", authority="IRCC", confidence="medium"),
        _p("ca_family_sponsorship", "CA", "family", "Family sponsorship", "information_hub", authority="IRCC", confidence="medium"),
        _p("ca_citizenship", "CA", "citizenship", "Canadian citizenship (grant)", "true_pathway", authority="IRCC", confidence="high"),
        _p("ca_proof_funds_crosscut", "CA", "cross_cutting", "Express Entry proof of funds", "cross_cutting_guidance", authority="IRCC", source_ids=[9083], confidence="high"),
    ]

    # -------- NZ --------
    rows += [
        _p("nz_visitor", "NZ", "short_term", "Visitor visa / NZeTA", "information_hub", authority="INZ", confidence="medium"),
        _p("nz_study_visas_hub", "NZ", "education", "Study visas hub", "information_hub", authority="INZ", source_ids=[11028], confidence="high"),
        _p("nz_student_visa", "NZ", "education", "Student visa", "true_pathway", authority="INZ", source_ids=[11028], confidence="medium"),
        _p("nz_work_visas_hub", "NZ", "employment", "Work visas hub", "information_hub", authority="INZ", source_ids=[9085], confidence="high"),
        _p("nz_accredited_employer_work", "NZ", "employment", "Accredited Employer Work Visa", "true_pathway", authority="INZ", confidence="high"),
        _p("nz_skilled_migrant", "NZ", "long_term_residence", "Skilled Migrant Category Resident Visa", "true_pathway", authority="INZ", source_ids=[11030], confidence="high"),
        _p("nz_residence_hub", "NZ", "long_term_residence", "Residence / live in NZ hub", "information_hub", authority="INZ", source_ids=[11029], confidence="high"),
        _p("nz_family_hub", "NZ", "family", "Bringing family hub", "information_hub", authority="INZ", source_ids=[11031], confidence="high"),
        _p("nz_entrepreneur", "NZ", "business", "Entrepreneur / investor residence candidates", "unknown_candidate", authority="INZ", confidence="low"),
        _p("nz_citizenship_by_grant", "NZ", "citizenship", "Citizenship by grant", "true_pathway", authority="DIA", source_ids=[11032], aliases=["nz_citizenship"], confidence="high"),
        _p("nz_citizenship_by_descent", "NZ", "citizenship", "Citizenship by descent", "true_pathway", authority="DIA", confidence="high", notes="Do not merge with grant eligibility"),
        _p("nz_citizenship_by_birth", "NZ", "citizenship", "Citizenship by birth", "true_pathway", authority="DIA", confidence="high", notes="Do not merge with grant eligibility"),
    ]

    # -------- IE --------
    rows += [
        _p("ie_short_stay", "IE", "short_term", "Short-stay visa", "true_pathway", authority="ISD/DFA", confidence="medium"),
        _p("ie_student", "IE", "education", "Study in Ireland / student permission", "true_pathway", authority="ISD", confidence="medium"),
        _p("ie_employment_permits_hub", "IE", "employment", "Employment permits hub", "information_hub", authority="DETE", source_ids=[9090], aliases=["ie_employment_permits"], confidence="high"),
        _p("ie_critical_skills_employment_permit", "IE", "employment", "Critical Skills Employment Permit", "true_pathway", authority="DETE", source_ids=[9090], parent_hub_id="ie_employment_permits_hub", confidence="high"),
        _p("ie_general_employment_permit", "IE", "employment", "General Employment Permit", "true_pathway", authority="DETE", source_ids=[9090], parent_hub_id="ie_employment_permits_hub", confidence="high"),
        _p("ie_intra_company_transfer", "IE", "employment", "Intra-Company Transfer Employment Permit", "true_pathway", authority="DETE", source_ids=[9090], confidence="medium"),
        _p("ie_coming_to_work_hub", "IE", "employment", "Coming to work in Ireland hub", "information_hub", authority="ISD", source_ids=[9089], confidence="high"),
        _p("ie_stamp_1g", "IE", "education", "Third Level Graduate Programme (Stamp 1G)", "true_pathway", authority="ISD", confidence="medium"),
        _p("ie_family_reunification", "IE", "family", "Family reunification", "true_pathway", authority="ISD", confidence="medium"),
        _p("ie_long_term_residence", "IE", "long_term_residence", "Long-term residence", "true_pathway", authority="ISD", confidence="medium"),
        _p("ie_citizenship", "IE", "citizenship", "Irish citizenship by naturalisation", "true_pathway", authority="ISD", confidence="medium"),
    ]

    # -------- CH --------
    rows += [
        _p("ch_short_stay", "CH", "short_term", "Short-stay Schengen / visitor", "true_pathway", authority="SEM", confidence="medium"),
        _p("ch_student", "CH", "education", "Student residence permit", "true_pathway", authority="SEM/cantons", confidence="medium"),
        _p("ch_work_labour_hub", "CH", "employment", "Work / labour market admission hub", "information_hub", authority="SEM", source_ids=[9091], confidence="high"),
        _p("ch_work_permit_non_eu", "CH", "employment", "Work permit for non-EU/EFTA nationals", "true_pathway", authority="SEM/cantons", source_ids=[9091], confidence="medium"),
        _p("ch_self_employed", "CH", "business", "Self-employment residence", "unknown_candidate", authority="SEM", confidence="low"),
        _p("ch_family", "CH", "family", "Family reunification", "true_pathway", authority="SEM", confidence="medium"),
        _p("ch_settlement_c", "CH", "long_term_residence", "Settlement permit (C)", "true_pathway", authority="SEM", confidence="high"),
        _p("ch_naturalisation", "CH", "citizenship", "Naturalisation", "true_pathway", authority="SEM/cantons", confidence="medium"),
    ]

    # -------- AE --------
    rows += [
        _p("ae_visit_visa", "AE", "short_term", "Visit / tourist visa", "true_pathway", authority="ICP / u.ae", confidence="medium", notes="No approved LifePilot source yet"),
        _p("ae_student", "AE", "education", "Student residence", "true_pathway", authority="ICP", confidence="medium"),
        _p("ae_work_residence", "AE", "employment", "Employment residence visa", "true_pathway", authority="MoHRE/ICP", confidence="medium"),
        _p("ae_golden_visa", "AE", "long_term_residence", "Golden Visa / long-term residence", "true_pathway", authority="ICP", confidence="medium"),
        _p("ae_green_visa", "AE", "long_term_residence", "Green Visa", "true_pathway", authority="ICP", confidence="medium"),
        _p("ae_investor", "AE", "business", "Investor / entrepreneur residence", "information_hub", authority="ICP", confidence="low"),
        _p("ae_family", "AE", "family", "Family sponsorship residence", "true_pathway", authority="ICP", confidence="medium"),
        _p("ae_citizenship", "AE", "citizenship", "UAE citizenship pathways", "unknown_candidate", authority="Cabinet/ICP", confidence="low", notes="Highly restricted; do not invent programs"),
    ]

    # -------- JP --------
    rows += [
        _p("jp_temporary_visitor", "JP", "short_term", "Temporary Visitor", "true_pathway", authority="MOFA/ISA", confidence="medium", notes="No approved LifePilot source yet"),
        _p("jp_student", "JP", "education", "Student status of residence", "true_pathway", authority="ISA", confidence="high"),
        _p("jp_engineer_specialist", "JP", "employment", "Engineer/Specialist in Humanities/International Services", "true_pathway", authority="ISA", confidence="high"),
        _p("jp_highly_skilled", "JP", "employment", "Highly Skilled Professional", "true_pathway", authority="ISA", confidence="high"),
        _p("jp_business_manager", "JP", "business", "Business Manager", "true_pathway", authority="ISA", confidence="medium"),
        _p("jp_specified_skilled", "JP", "employment", "Specified Skilled Worker", "true_pathway", authority="ISA", confidence="medium"),
        _p("jp_family", "JP", "family", "Dependent / family statuses", "information_hub", authority="ISA", confidence="medium"),
        _p("jp_permanent_resident", "JP", "long_term_residence", "Permanent Resident", "true_pathway", authority="ISA", confidence="high"),
        _p("jp_naturalisation", "JP", "citizenship", "Naturalisation", "true_pathway", authority="MOJ", confidence="medium"),
    ]

    # -------- FR --------
    rows += [
        _p("fr_short_stay", "FR", "short_term", "Short-stay Schengen visa", "true_pathway", authority="France-Visas / Interior", confidence="medium", notes="No approved LifePilot source yet"),
        _p("fr_student", "FR", "education", "Student residence permit", "true_pathway", authority="Interior / Campus France", confidence="high"),
        _p("fr_passeport_talent", "FR", "employment", "Passeport talent", "true_pathway", authority="Interior", confidence="high"),
        _p("fr_salarie", "FR", "employment", "Salarié residence permit", "true_pathway", authority="Interior", confidence="medium"),
        _p("fr_entreprise", "FR", "business", "Entrepreneur / business creator", "true_pathway", authority="Interior", confidence="medium"),
        _p("fr_family", "FR", "family", "Family reunification / vie privée et familiale", "information_hub", authority="Interior", confidence="medium"),
        _p("fr_carte_resident", "FR", "long_term_residence", "Carte de résident", "true_pathway", authority="Interior", confidence="high"),
        _p("fr_naturalisation", "FR", "citizenship", "Naturalisation", "true_pathway", authority="Interior", confidence="high"),
    ]

    # -------- EU --------
    rows += [
        _p("eu_short_stay_schengen", "EU", "short_term", "Schengen short-stay (cross-border rules)", "cross_cutting_guidance", authority="EU / Your Europe", confidence="medium"),
        _p("eu_blue_card_framework", "EU", "employment", "EU Blue Card Directive framework", "cross_cutting_guidance", authority="EU", confidence="medium", notes="Implemented nationally; not a substitute for DE/NL national pages"),
        _p("eu_long_term_resident_directive", "EU", "long_term_residence", "EU long-term resident status (Directive)", "cross_cutting_guidance", authority="EU", confidence="medium"),
        _p("eu_residence_hub", "EU", "long_term_residence", "Your Europe residence hub", "information_hub", authority="Your Europe", source_ids=[91], confidence="medium"),
        _p("eu_family_reunification_framework", "EU", "family", "Family reunification Directive framework", "cross_cutting_guidance", authority="EU", confidence="low"),
    ]

    # Category completeness: ensure every jurisdiction×major category has at least a slot
    seen = {(r["country"], r["category"]) for r in rows}
    for country in (
        "GB", "DE", "NL", "SG", "US", "AU", "CA", "NZ", "IE", "CH", "AE", "JP", "FR", "EU"
    ):
        for cat in (
            "short_term",
            "education",
            "employment",
            "business",
            "family",
            "long_term_residence",
            "citizenship",
        ):
            if country == "EU" and cat in {"citizenship", "business"}:
                continue
            if (country, cat) not in seen:
                rows.append(
                    _p(
                        f"{country.lower()}_unknown_{cat}",
                        country,
                        cat,
                        f"{country} {cat} — not yet researched",
                        "unknown_candidate",
                        confidence="low",
                        notes="Explicit unknown slot so category remains in denominator",
                    )
                )
                seen.add((country, cat))

    return rows
