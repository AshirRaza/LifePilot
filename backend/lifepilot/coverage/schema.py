"""Enums and dataclasses for the Phase 6E coverage audit."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class PathwayStatus(str, Enum):
    VERIFIED = "verified"
    CANDIDATE = "candidate"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN_OR_NOT_INVENTORIED = "unknown_or_not_inventoried"


class CoverageClass(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNEVALUATED = "UNEVALUATED"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RemediationType(str, Enum):
    DISCOVERY = "discovery"
    EXTRACTION = "extraction"
    ALLOWLIST_REVIEW = "allowlist_review"
    HUMAN_VERIFY = "human_verify"
    BLOCKED_SOURCE = "blocked_source"


@dataclass(frozen=True)
class EvidenceRef:
    document_id: int
    version_id: int
    extractor: str
    heading_or_excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PathwayRecord:
    pathway_id: str
    country: str
    parent_category: str
    name: str
    status: PathwayStatus
    source_ids: list[int] = field(default_factory=list)
    document_ids: list[int] = field(default_factory=list)
    evidence_thin: bool = True
    financial_applicable: bool = True
    notes: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pathway_id": self.pathway_id,
            "country": self.country,
            "parent_category": self.parent_category,
            "name": self.name,
            "status": self.status.value,
            "source_ids": list(self.source_ids),
            "document_ids": list(self.document_ids),
            "evidence_thin": self.evidence_thin,
            "financial_applicable": self.financial_applicable,
            "notes": list(self.notes),
            "provenance": dict(self.provenance),
        }


@dataclass
class RequirementCell:
    pathway_id: str
    country: str
    requirement_field: str
    coverage_class: CoverageClass
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    needs_human_review: bool = False
    notes: list[str] = field(default_factory=list)
    is_critical: bool = False
    applicable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "pathway_id": self.pathway_id,
            "country": self.country,
            "requirement_field": self.requirement_field,
            "coverage_class": self.coverage_class.value,
            "evidence_refs": [e.to_dict() for e in self.evidence_refs],
            "needs_human_review": self.needs_human_review,
            "notes": list(self.notes),
            "is_critical": self.is_critical,
            "applicable": self.applicable,
        }


@dataclass
class GapRow:
    gap_id: str
    country: str
    pathway_id: str
    requirement_field: str | None
    severity: Severity
    coverage_class: CoverageClass
    summary: str
    source_ids: list[int] = field(default_factory=list)
    document_ids: list[int] = field(default_factory=list)
    remediation_type: RemediationType = RemediationType.DISCOVERY
    existing_evidence: str = ""
    potential_lead_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "country": self.country,
            "pathway_id": self.pathway_id,
            "requirement_field": self.requirement_field or "",
            "severity": self.severity.value,
            "coverage_class": self.coverage_class.value,
            "summary": self.summary,
            "source_ids": ",".join(str(s) for s in self.source_ids),
            "document_ids": ",".join(str(d) for d in self.document_ids),
            "remediation_type": self.remediation_type.value,
            "existing_evidence": self.existing_evidence,
            "potential_lead_url": self.potential_lead_url or "",
        }


@dataclass
class CountryScore:
    country: str
    parent_slots_considered: int
    pathways_inventoried: int
    pathways_with_any_evidence: int
    pathways_verified: int
    pathways_ready: int
    requirement_cells_total: int
    requirement_cells_complete: int
    requirement_cells_partial: int
    requirement_cells_missing: int
    requirement_cells_blocked: int
    requirement_cells_unevaluated: int
    requirement_cells_not_applicable: int
    critical_applicable_total: int
    critical_complete: int
    covered_rate: float
    critical_complete_rate: float
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d
