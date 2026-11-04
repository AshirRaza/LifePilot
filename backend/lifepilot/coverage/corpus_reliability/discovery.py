"""Targeted official-source discovery candidates (no fetches)."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def build_discovery_candidates() -> list[dict[str, Any]]:
    """Exact pathway-specific official URL candidates awaiting owner approval.

    Do not fetch these unless already authorized under existing approval rules.
    Avoid known 404s and generic hubs as pathway-specific evidence.
    """


    return [
        {
            "jurisdiction": "JP",
            "pathway": "jp_engineer_specialist",
            "authority": "Immigration Services Agency (MOJ)",
            "url": "https://www.moj.go.jp/isa/applications/status/engineer.html",
            "notes": "Engineer/Specialist in Humanities/International Services status page",
            "approval_status": "pending_new_source_approval",
            "already_authorized": False,
            "do_not_fetch": True,
        },
        {
            "jurisdiction": "FR",
            "pathway": "fr_passeport_talent",
            "authority": "Service-Public.fr",
            "url": "https://www.service-public.fr/particuliers/vosdroits/N19804",
            "notes": "Passeport Talent official rights page (N19804)",
            "approval_status": "pending_new_source_approval",
            "already_authorized": False,
            "do_not_fetch": True,
        },
        {
            "jurisdiction": "AE",
            "pathway": "ae_work_residence",
            "authority": "u.ae / ICP",
            "url": "https://u.ae/en/information-and-services/jobs/working-in-the-uae",
            "notes": (
                "Prefer work-specific guidance over the generic visa-and-emirates-id hub; "
                "confirm exact residence/work pathway page before approval"
            ),
            "approval_status": "pending_new_source_approval",
            "already_authorized": False,
            "do_not_fetch": True,
        },
        {
            "jurisdiction": "CA",
            "pathway": "ca_express_entry_programs",
            "authority": "IRCC",
            "url": "",
            "notes": (
                "Express Entry post-invitation document checklist URL still unresolved; "
                "do not assert a URL. Retain Federal Skilled Trades work-hours discrepancy "
                "as an explicit open issue until official text is reconciled."
            ),
            "approval_status": "pending_url_research",
            "already_authorized": False,
            "do_not_fetch": True,
            "open_issue": "ca_federal_skilled_trades_work_hours_discrepancy",
        },
        {
            "jurisdiction": "NZ",
            "pathway": "nz_citizenship_by_grant",
            "authority": "Department of Internal Affairs",
            "url": "https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/",
            "notes": "Citizenship by grant application guidance (govt.nz)",
            "approval_status": "pending_new_source_or_allowlist_approval",
            "already_authorized": False,
            "do_not_fetch": True,
        },
    ]


def write_discovery_csv(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        path.write_text(
            "jurisdiction,pathway,authority,url,notes,approval_status,already_authorized,do_not_fetch,open_issue\n",
            encoding="utf-8",
        )
        return
    fieldnames = [
        "jurisdiction", "pathway", "authority", "url", "notes",
        "approval_status", "already_authorized", "do_not_fetch", "open_issue",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
