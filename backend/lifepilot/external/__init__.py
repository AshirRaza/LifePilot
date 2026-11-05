"""Phase 7: external immigration knowledge integrations (read-only).

External evidence is always ``unverified_external`` and never mutates
internal coverage classifications, readiness, or source allowlists.
"""

from lifepilot.external.models import (
    ExternalEvidence,
    ExternalQuery,
    ProviderId,
    RetrievalResult,
    RetrievalStatus,
    SourceType,
    VerificationState,
)
from lifepilot.external.router import ExternalKnowledgeRouter, query_external

__all__ = [
    "ExternalEvidence",
    "ExternalKnowledgeRouter",
    "ExternalQuery",
    "ProviderId",
    "RetrievalResult",
    "RetrievalStatus",
    "SourceType",
    "VerificationState",
    "query_external",
]
