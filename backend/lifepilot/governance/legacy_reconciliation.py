"""Dry-run reconciliation of legacy LegalRule / JSON review flags (Phase 10D.4).

Never creates approval events from legacy statuses. Never mutates live data
unless explicitly executed (default dry-run).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.db.models import LegalRule
from lifepilot.governance.approval_invariant import validate_legal_rule_human_authority
from lifepilot.governance.high_risk_queue_import import load_high_risk_queue


@dataclass
class LegacyFinding:
    kind: str
    identifier: str
    reason: str
    severity: str = "high"


@dataclass
class LegacyReconciliationReport:
    generated_at: str
    dry_run: bool
    findings: list[LegacyFinding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "dry_run": self.dry_run,
            "counts": {
                "total": len(self.findings),
                "by_kind": {
                    k: sum(1 for f in self.findings if f.kind == k)
                    for k in sorted({f.kind for f in self.findings})
                },
            },
            "findings": [
                {
                    "kind": f.kind,
                    "identifier": f.identifier,
                    "reason": f.reason,
                    "severity": f.severity,
                }
                for f in self.findings
            ],
            "policy": (
                "Legacy human_verified flags and JSON statuses are operational "
                "context only and do not independently establish legal approval."
            ),
        }


def reconcile_legacy_governance(
    session: Session,
    *,
    dry_run: bool = True,
    include_json_queue: bool = True,
) -> LegacyReconciliationReport:
    """Inspect LegalRule rows and optional JSON queue for incomplete governance."""
    report = LegacyReconciliationReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        dry_run=dry_run,
    )
    rules = list(session.scalars(select(LegalRule)).all())
    for rule in rules:
        if rule.verification_status != "human_verified":
            continue
        check = validate_legal_rule_human_authority(session, rule)
        if check.ok:
            continue
        report.findings.append(
            LegacyFinding(
                kind="orphan_human_verified_legal_rule",
                identifier=f"legal_rule:{rule.id}:{rule.rule_key}",
                reason=";".join(check.reasons) or "invariant_failed",
                severity="critical",
            )
        )
        if not dry_run:
            from lifepilot.governance.legal_rule_promotion import (
                demote_legal_rule_authority,
            )

            demote_legal_rule_authority(
                session,
                legal_rule_id=rule.id,
                reason="legacy_reconciliation:" + ",".join(check.reasons),
            )

        if rule.document_version_id is None:
            report.findings.append(
                LegacyFinding(
                    kind="missing_pinned_version",
                    identifier=f"legal_rule:{rule.id}",
                    reason="human_verified without document_version_id",
                )
            )
        if not (rule.reviewed_by or "").strip():
            report.findings.append(
                LegacyFinding(
                    kind="incomplete_reviewer_identity",
                    identifier=f"legal_rule:{rule.id}",
                    reason="missing reviewed_by",
                )
            )

    if include_json_queue:
        try:
            queue = load_high_risk_queue()
        except Exception as exc:  # noqa: BLE001
            report.findings.append(
                LegacyFinding(
                    kind="json_queue_unreadable",
                    identifier="high_risk_legal_review_queue",
                    reason=str(exc),
                    severity="medium",
                )
            )
            queue = {}
        for item in queue.get("items") or []:
            status = str(item.get("verification_status") or "")
            if status == "human_verified":
                report.findings.append(
                    LegacyFinding(
                        kind="json_human_verified_non_authoritative",
                        identifier=str(item.get("id") or "?"),
                        reason=(
                            "JSON human_verified is operational only; "
                            "not a LegalRule approval event"
                        ),
                        severity="medium",
                    )
                )
            if not item.get("document_version_id") and not item.get(
                "evidence_fingerprint"
            ):
                report.findings.append(
                    LegacyFinding(
                        kind="json_missing_evidence_provenance",
                        identifier=str(item.get("id") or "?"),
                        reason="insufficient provenance for DB import",
                        severity="low",
                    )
                )

    return report
