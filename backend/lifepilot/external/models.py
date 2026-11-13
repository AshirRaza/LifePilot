"""Shared models for external knowledge retrieval."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ProviderId(str, Enum):
    GOVUK_CONTENT = "govuk_content"
    CANADA_OPEN = "canada_open"
    EU_OPENDATA = "eu_opendata"
    EURLEX_CELLAR = "eurlex_cellar"
    US_FEDERAL_REGISTER = "us_federal_register"
    EU_IMMIGRATION_PORTAL = "eu_immigration_portal"
    NATIONAL_OFFICIAL = "national_official"


class RetrievalStatus(str, Enum):
    OK = "OK"
    NO_RESULTS = "NO_RESULTS"
    UNAVAILABLE = "UNAVAILABLE"
    BLOCKED = "BLOCKED"
    OWNER_ACTION_REQUIRED = "OWNER_ACTION_REQUIRED"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    TIMEOUT = "TIMEOUT"


class SourceType(str, Enum):
    GUIDANCE = "guidance"
    LEGISLATION = "legislation"
    DATASET = "dataset"
    NOTICE = "notice"
    PROPOSED_RULE = "proposed_rule"
    FINAL_RULE = "final_rule"
    OTHER = "other"


class VerificationState(str, Enum):
    """External results are never treated as verified pathway evidence."""

    UNVERIFIED_EXTERNAL = "unverified_external"


@dataclass(frozen=True)
class ExternalQuery:
    """Deterministic retrieval request for the external knowledge router."""

    query: str = ""
    jurisdiction: str | None = None  # e.g. GB, CA, US, EU
    pathway_id: str | None = None
    requirement: str | None = None  # eligibility, required_documents, ...
    providers: tuple[ProviderId, ...] | None = None
    published_after: str | None = None  # YYYY-MM-DD
    published_before: str | None = None
    max_results_per_provider: int = 5
    language: str | None = None


@dataclass
class ExternalEvidence:
    """One external evidence item with provenance. Unverified by default."""

    provider: ProviderId
    title: str
    official_url: str
    source_type: SourceType
    jurisdiction: str | None = None
    pathway_id: str | None = None
    requirement: str | None = None
    canonical_url: str | None = None
    document_id: str | None = None
    passages: list[str] = field(default_factory=list)
    published_at: str | None = None
    updated_at: str | None = None
    effective_at: str | None = None
    retrieved_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    language: str | None = None
    license: str | None = None
    legal_status: str | None = None
    verification_state: VerificationState = VerificationState.UNVERIFIED_EXTERNAL
    retrieval_status: RetrievalStatus = RetrievalStatus.OK
    error_detail: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def dedupe_key(self) -> str:
        base = (self.canonical_url or self.official_url or "").strip().lower()
        doc = (self.document_id or "").strip().lower()
        return f"{self.provider.value}|{doc}|{base}"


@dataclass
class ProviderOutcome:
    provider: ProviderId
    status: RetrievalStatus
    items: list[ExternalEvidence] = field(default_factory=list)
    error_detail: str | None = None
    capabilities: list[str] = field(default_factory=list)


@dataclass
class RetrievalResult:
    query: ExternalQuery
    outcomes: list[ProviderOutcome] = field(default_factory=list)
    items: list[ExternalEvidence] = field(default_factory=list)
    retrieved_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def ok_providers(self) -> list[ProviderId]:
        return [o.provider for o in self.outcomes if o.status == RetrievalStatus.OK]

    @property
    def failed_providers(self) -> list[ProviderOutcome]:
        return [o for o in self.outcomes if o.status != RetrievalStatus.OK]
