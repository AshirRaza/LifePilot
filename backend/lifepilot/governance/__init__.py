"""Human legal review governance (Phase 10D).

Workflow and authorization live here; persistence primitives remain in
``lifepilot.db.legal_review``.
"""

from lifepilot.governance.approval_invariant import (
    ApprovalValidationResult,
    legal_rule_is_authoritative,
    validate_legal_rule_human_authority,
)
from lifepilot.governance.legal_rule_promotion import (
    demote_legal_rule_authority,
    promote_legal_rule_from_approval,
)
from lifepilot.governance.reassessment import build_reassessment_queue
from lifepilot.governance.review_workflow import ReviewWorkflowService
from lifepilot.governance.reviewer_auth import (
    PERM_LEGAL_REVIEW_ADMIN,
    PERM_LEGAL_REVIEW_ASSIGN,
    PERM_LEGAL_REVIEW_DECIDE,
    PERM_LEGAL_REVIEW_READ,
    ReviewerAuthError,
    ReviewerIdentity,
    get_reviewer_auth_provider,
    reset_reviewer_auth_provider_cache,
)
from lifepilot.governance.source_change import (
    SourceChangeMateriality,
    process_source_version_change,
)
from lifepilot.governance.verification_policy import (
    LegalVerificationPolicy,
    get_legal_verification_policy,
    resolve_effective_policy,
)

__all__ = [
    "ApprovalValidationResult",
    "LegalVerificationPolicy",
    "PERM_LEGAL_REVIEW_ADMIN",
    "PERM_LEGAL_REVIEW_ASSIGN",
    "PERM_LEGAL_REVIEW_DECIDE",
    "PERM_LEGAL_REVIEW_READ",
    "ReviewWorkflowService",
    "ReviewerAuthError",
    "ReviewerIdentity",
    "SourceChangeMateriality",
    "build_reassessment_queue",
    "demote_legal_rule_authority",
    "get_legal_verification_policy",
    "get_reviewer_auth_provider",
    "legal_rule_is_authoritative",
    "process_source_version_change",
    "promote_legal_rule_from_approval",
    "reset_reviewer_auth_provider_cache",
    "resolve_effective_policy",
    "validate_legal_rule_human_authority",
]
