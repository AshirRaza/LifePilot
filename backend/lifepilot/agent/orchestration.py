"""Evidence orchestration: internal hybrid RAG + external connectors."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Sequence

from sqlalchemy.orm import Session, sessionmaker

from lifepilot.agent.config import AgentConfig, get_agent_config
from lifepilot.agent.contracts import AgentStatus
from lifepilot.agent.evidence import (
    EvidenceItem,
    evidence_from_external_item,
    evidence_from_internal_passage,
    evidence_from_legal_rule,
)
from lifepilot.agent.retrieval_policy import (
    ExternalMode,
    RetrievalDecision,
    assess_internal_freshness,
    decide_retrieval,
    detect_high_risk_categories,
    reconcile_effective_dates,
    resolve_external_mode,
)
from lifepilot.agent.temporal_dates import detect_question_temporal_intent
from lifepilot.agent.temporal_selection import (
    TemporalOutcomeStatus,
    TemporalQueryMode,
    resolve_temporal_context,
    select_temporal_evidence,
)
from lifepilot.rag.legal_rules import (
    LegalRuleLookupResult,
    query_reviewed_legal_rules,
)
from lifepilot.external.http import ExternalHttpClient, HttpClientConfig
from lifepilot.external.mapping import PROVIDER_JURISDICTIONS, resolve_jurisdiction
from lifepilot.external.models import (
    ExternalEvidence,
    ExternalQuery,
    ProviderId,
    ProviderOutcome,
    RetrievalStatus as ExternalRetrievalStatus,
)
from lifepilot.external.router import (
    ALL_ALLOWED_HOSTS,
    ExternalKnowledgeRouter,
    dedupe_evidence,
)
from lifepilot.rag.embedder import Embedder, get_shared_embedder
from lifepilot.rag.retrieval import (
    InternalKnowledgeRetriever,
    InternalQuery,
    RetrievalMode,
    RetrievalStatus as InternalRetrievalStatus,
    RetrievedPassage,
)

logger = logging.getLogger(__name__)

# EU-scoped connectors / jurisdictions stay out of non-EU national requests.
EU_PROVIDER_IDS = frozenset(
    {
        ProviderId.EU_OPENDATA,
        ProviderId.EURLEX_CELLAR,
        ProviderId.EU_IMMIGRATION_PORTAL,
    }
)
EU_RELATED_JURISDICTIONS = frozenset({"EU", "DE", "NL", "FR", "IE", "CH"})

PROVIDER_LIMITATION_NOTES: dict[str, str] = {
    ProviderId.GOVUK_CONTENT.value: (
        "GOV.UK Content API returns UK guidance pages, not a complete eligibility determination."
    ),
    ProviderId.CANADA_OPEN.value: (
        "Canada Open Government returns datasets; not comprehensive individual visa eligibility."
    ),
    ProviderId.EU_OPENDATA.value: (
        "EU Open Data returns datasets, not national immigration eligibility rules."
    ),
    ProviderId.EURLEX_CELLAR.value: (
        "EUR-Lex/CELLAR returns EU legislation, not automatic national implementation."
    ),
    ProviderId.US_FEDERAL_REGISTER.value: (
        "US Federal Register covers regulations/notices, not comprehensive visa guidance."
    ),
    ProviderId.EU_IMMIGRATION_PORTAL.value: (
        "EU Immigration Portal has no unrestricted API; results are limited and unapproved for ingestion."
    ),
    ProviderId.NATIONAL_OFFICIAL.value: (
        "National official HTML pages are curated allowlisted fetches; "
        "unverified_external until approved through the knowledge-base workflow."
    ),
}

MIN_SUBSTANTIVE_CHARS = 40


@dataclass
class OrchestrationResult:
    evidence: list[EvidenceItem] = field(default_factory=list)
    status: AgentStatus = AgentStatus.SUCCESS
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    internal_ms: float = 0.0
    external_ms: float = 0.0
    normalize_ms: float = 0.0
    internal_status: str | None = None
    external_provider_outcomes: list[dict[str, Any]] = field(default_factory=list)
    profile_ms: dict[str, float] = field(default_factory=dict)
    retrieval_decision: str | None = None
    retrieval_decision_reasons: list[str] = field(default_factory=list)
    resolved_reference_date: str | None = None
    temporal_mode: str | None = None
    temporal_outcome: str | None = None
    excluded_evidence: list[dict[str, Any]] = field(default_factory=list)


def _is_substantive(text: str) -> bool:
    return len((text or "").strip()) >= MIN_SUBSTANTIVE_CHARS


_MONEY_RE = re.compile(r"(?:£|\$|€)\s?\d[\d,]*(?:\.\d+)?")
_HISTORICAL_EXTERNAL_PROVIDERS = frozenset({ProviderId.US_FEDERAL_REGISTER})


def _numeric_conflict_warnings(evidence: list[EvidenceItem]) -> list[str]:
    """Detect conflicting monetary/rate values, currencies, or units across sources."""
    from lifepilot.agent.evidence import EvidenceSourceType
    from lifepilot.agent.numeric import (
        NumericParseStatus,
        ParsedNumericValue,
        quantities_conflict,
        quantities_from_evidence_item,
    )

    internal_qs: list[ParsedNumericValue] = []
    external_qs: list[ParsedNumericValue] = []
    rule_qs: list[ParsedNumericValue] = []

    def _money_or_rate(item: EvidenceItem) -> list[ParsedNumericValue]:
        return [
            q
            for q in quantities_from_evidence_item(item)
            if q.status == NumericParseStatus.PARSED
            and (q.currency_code or q.currency_ambiguous)
        ]

    for item in evidence:
        found = _money_or_rate(item)
        if not found:
            continue
        if item.source_type == EvidenceSourceType.INTERNAL_RAG:
            internal_qs.extend(found)
        elif item.source_type == EvidenceSourceType.EXTERNAL_API:
            external_qs.extend(found)
        elif item.source_type == EvidenceSourceType.CURATED_RULE:
            rule_qs.extend(found)

    def _fmt(qs: list[ParsedNumericValue]) -> list[str]:
        seen: list[str] = []
        for q in qs:
            lbl = q.raw_literal
            if lbl not in seen:
                seen.append(lbl)
            if len(seen) >= 5:
                break
        return seen

    def _has_compatible_pair(a: list[ParsedNumericValue], b: list[ParsedNumericValue]) -> bool:
        return any(not quantities_conflict(qa, qb) for qa in a for qb in b)

    warnings: list[str] = []
    if internal_qs and external_qs and not _has_compatible_pair(internal_qs, external_qs):
        warnings.append(
            "possible_internal_external_numeric_conflict:"
            f"internal={_fmt(internal_qs)};external={_fmt(external_qs)};"
            "do_not_treat_as_settled_without_review"
        )
    other_qs = internal_qs + external_qs
    if rule_qs and other_qs and not _has_compatible_pair(rule_qs, other_qs):
        warnings.append(
            "possible_curated_rule_vs_passage_numeric_conflict:"
            f"rule={_fmt(rule_qs)};passage={_fmt(other_qs)}"
        )
    return warnings


def _deadline_remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.perf_counter())


def explicit_providers_for_jurisdiction(jurisdiction: str) -> list[ProviderId]:
    """Return only explicitly mapped providers (no all-provider fallback)."""
    jur = (jurisdiction or "").strip().upper()
    if jur == "UK":
        jur = "GB"
    if not jur:
        return []
    selected = [
        pid
        for pid, jurisdictions in PROVIDER_JURISDICTIONS.items()
        if jur in jurisdictions
    ]
    # Prefer curated national pages ahead of EU open-data connectors.
    selected.sort(
        key=lambda p: (0 if p == ProviderId.NATIONAL_OFFICIAL else 1, p.value)
    )
    return selected


class EvidenceOrchestrator:
    """Gather and normalize evidence for one agent request."""

    def __init__(
        self,
        *,
        config: AgentConfig | None = None,
        session_factory: sessionmaker[Session] | None = None,
        embedder: Embedder | None = None,
        external_router: ExternalKnowledgeRouter | None = None,
        internal_retriever_factory: Callable[..., InternalKnowledgeRetriever]
        | None = None,
    ) -> None:
        self.config = config or get_agent_config()
        self._session_factory = session_factory
        self._embedder = embedder
        self._external_router = external_router
        self._owns_router = external_router is None
        self._internal_retriever_factory = internal_retriever_factory
        self._last_internal_profile: dict[str, float] = {}

    def close(self) -> None:
        if self._owns_router and self._external_router is not None:
            self._external_router.close()
            self._external_router = None

    def gather(
        self,
        *,
        question: str,
        jurisdiction: str,
        pathway_id: str | None = None,
        retrieval_mode: str = "hybrid",
        top_k: int = 8,
        include_external: bool | None = None,
        external_mode: str | None = None,
        request_id: str | None = None,
        deadline_monotonic: float | None = None,
        as_of_date: date | None = None,
        temporal_mode: str | None = None,
        user_circumstances: dict[str, Any] | None = None,
        period_start: date | None = None,
        period_end: date | None = None,
        temporal_period_year: int | None = None,
        clarification_needed: bool = False,
    ) -> OrchestrationResult:
        cfg = self.config
        jur = (jurisdiction or "").strip().upper()
        if not jur:
            return OrchestrationResult(
                status=AgentStatus.INVALID_REQUEST,
                errors=["jurisdiction is required before retrieval"],
            )

        if temporal_mode is None and period_start is None and not clarification_needed:
            intent = detect_question_temporal_intent(
                question, explicit_as_of_date=as_of_date
            )
            resolved_ref_date: date | None = intent.resolved_reference_date
            resolved_t_mode = intent.temporal_mode
            period_start = intent.period_start
            period_end = intent.period_end
            temporal_period_year = intent.period_year
            clarification_needed = intent.clarification_required
            intent_warnings = list(intent.warnings)
            intent_missing = list(intent.missing_information)
        else:
            intent_warnings = []
            intent_missing = []
            if period_start is not None or clarification_needed:
                resolved_ref_date = as_of_date
                resolved_t_mode = (
                    TemporalQueryMode(str(temporal_mode))
                    if temporal_mode in {m.value for m in TemporalQueryMode}
                    else TemporalQueryMode.HISTORICAL
                )
            else:
                resolved_ref_date, resolved_t_mode = resolve_temporal_context(as_of_date)
                if temporal_mode in {m.value for m in TemporalQueryMode} and as_of_date is None:
                    resolved_t_mode = TemporalQueryMode(str(temporal_mode))

        is_historical_query = resolved_t_mode == TemporalQueryMode.HISTORICAL

        mode: RetrievalMode = (
            retrieval_mode
            if retrieval_mode in ("semantic", "keyword", "hybrid")
            else "hybrid"
        )
        capped_top_k = max(1, min(int(top_k), cfg.max_retrieval_top_k, cfg.max_evidence_items))
        resolved_mode = resolve_external_mode(
            external_mode=external_mode,
            include_external=include_external,
        )
        warnings: list[str] = [
            "Retrieved evidence does not establish complete visa eligibility or "
            "COMPLETE pathway coverage.",
        ]
        warnings.extend(intent_warnings)
        if pathway_id:
            warnings.append(
                "pathway_id was not used as a chunk filter; curated pathway tags "
                "are unavailable and applicability is not inferred."
            )

        if clarification_needed:
            return OrchestrationResult(
                evidence=[],
                status=AgentStatus.INSUFFICIENT_EVIDENCE,
                warnings=list(dict.fromkeys(warnings + ["temporal_clarification_required"])),
                missing_information=list(
                    dict.fromkeys(intent_missing or ["unambiguous_as_of_date_YYYY_MM_DD"])
                ),
                retrieval_decision=RetrievalDecision.INTERNAL_ONLY.value,
                resolved_reference_date=None,
                temporal_mode=resolved_t_mode.value,
                temporal_outcome=TemporalOutcomeStatus.INSUFFICIENT_FOR_DATE.value,
            )

        if deadline_monotonic is None:
            deadline_monotonic = time.perf_counter() + float(cfg.request_timeout_seconds)

        remaining = _deadline_remaining(deadline_monotonic)
        if remaining is not None and remaining <= 0:
            return OrchestrationResult(
                status=AgentStatus.EXECUTION_FAILURE,
                errors=["request deadline exceeded before evidence orchestration"],
                warnings=warnings,
                resolved_reference_date=(
                    resolved_ref_date.isoformat() if resolved_ref_date else None
                ),
                temporal_mode=resolved_t_mode.value,
            )

        internal_passages: list[RetrievedPassage] = []
        internal_status = "skipped"
        internal_ms = 0.0
        external_items: list[ExternalEvidence] = []
        external_outcomes: list[ProviderOutcome] = []
        external_ms = 0.0
        errors: list[str] = []
        decision_meta: list[str] = []
        retrieval_decision = RetrievalDecision.INTERNAL_ONLY.value

        rule_lookup = self._retrieve_curated_rules(
            jurisdiction=jur,
            as_of_date=resolved_ref_date,
            pathway_id=pathway_id,
            user_circumstances=user_circumstances,
            period_start=period_start,
            period_end=period_end,
        )
        warnings.extend(rule_lookup.warnings)

        explicit_providers = explicit_providers_for_jurisdiction(jur)
        connectors_available = bool(explicit_providers)
        historical_providers = [
            p for p in explicit_providers if p in _HISTORICAL_EXTERNAL_PROVIDERS
        ]

        # Explicit ON: parallel internal + external for latency.
        if resolved_mode == ExternalMode.ON:
            if is_historical_query and not historical_providers:
                if connectors_available:
                    warnings.append(
                        "external_current_connectors_skipped_for_historical_as_of_date"
                    )
                try:
                    internal_passages, internal_status, internal_ms = self._retrieve_internal(
                        question=question,
                        jurisdiction=jur,
                        mode=mode,
                        top_k=min(cfg.max_retrieval_top_k, max(capped_top_k, 16)),
                        deadline_monotonic=deadline_monotonic,
                        include_historical=is_historical_query,
                        as_of_date=resolved_ref_date,
                        temporal_mode=resolved_t_mode.value,
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"internal_retrieval_failed:{exc.__class__.__name__}")
                    internal_status = "error"
                    logger.exception(
                        "agent.internal_retrieval_error request_id=%s", request_id
                    )
                retrieval_decision = RetrievalDecision.INTERNAL_ONLY.value
                decision_meta = ["external_mode=on_blocked_for_historical"]
            else:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    fut_internal = pool.submit(
                        self._retrieve_internal,
                        question=question,
                        jurisdiction=jur,
                        mode=mode,
                        top_k=capped_top_k,
                        deadline_monotonic=deadline_monotonic,
                        include_historical=is_historical_query,
                        as_of_date=resolved_ref_date,
                        temporal_mode=resolved_t_mode.value,
                    )
                    fut_external = pool.submit(
                        self._retrieve_external,
                        question=question,
                        jurisdiction=jur,
                        pathway_id=pathway_id,
                        deadline_monotonic=deadline_monotonic,
                        as_of_date=(
                            (resolved_ref_date or period_end)
                            if is_historical_query
                            else None
                        ),
                        historical_only=is_historical_query,
                    )
                    try:
                        internal_passages, internal_status, internal_ms = fut_internal.result(
                            timeout=_deadline_remaining(deadline_monotonic)
                        )
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"internal_retrieval_failed:{exc.__class__.__name__}")
                        internal_status = "error"
                        logger.exception(
                            "agent.internal_retrieval_error request_id=%s", request_id
                        )
                    try:
                        external_items, external_outcomes, external_ms = fut_external.result(
                            timeout=_deadline_remaining(deadline_monotonic)
                        )
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"external_retrieval_failed:{exc.__class__.__name__}")
                        logger.exception(
                            "agent.external_retrieval_error request_id=%s", request_id
                        )
                retrieval_decision = RetrievalDecision.INTERNAL_PLUS_EXTERNAL.value
                decision_meta = ["external_mode=on"]
        else:
            # OFF and AUTO: internal first.
            internal_top_k = (
                min(cfg.max_retrieval_top_k, max(capped_top_k, 16))
                if is_historical_query
                else capped_top_k
            )
            try:
                internal_passages, internal_status, internal_ms = self._retrieve_internal(
                    question=question,
                    jurisdiction=jur,
                    mode=mode,
                    top_k=internal_top_k,
                    deadline_monotonic=deadline_monotonic,
                    include_historical=is_historical_query,
                    as_of_date=resolved_ref_date,
                    temporal_mode=resolved_t_mode.value,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"internal_retrieval_failed:{exc.__class__.__name__}")
                internal_status = "error"
                logger.exception(
                    "agent.internal_retrieval_error request_id=%s", request_id
                )

            internal_passages = [
                p for p in internal_passages if p.jurisdiction_code == jur
            ]
            substantive_internal = sum(
                1 for p in internal_passages if _is_substantive(p.content_text)
            )
            version_ages: dict[int, datetime] = {}
            missing_ver_ids: list[int | None] = []
            for p in internal_passages:
                raw_ts = p.last_checked_at or p.retrieved_at
                if raw_ts and p.document_version_id is not None:
                    try:
                        dt = datetime.fromisoformat(raw_ts)
                        version_ages[int(p.document_version_id)] = dt
                        continue
                    except ValueError:
                        pass
                missing_ver_ids.append(p.document_version_id)
            if missing_ver_ids:
                version_ages.update(self._lookup_version_retrieved_at(missing_ver_ids))
            freshness = assess_internal_freshness(
                urls=[p.official_url for p in internal_passages],
                version_retrieved_at=version_ages,
                version_ids=[p.document_version_id for p in internal_passages],
                question_categories=detect_high_risk_categories(question),
            )
            policy = decide_retrieval(
                question=question,
                jurisdiction=jur,
                pathway_id=pathway_id,
                external_mode=resolved_mode,
                substantive_internal_count=substantive_internal,
                internal_status=internal_status,
                freshness=freshness,
                connectors_available=connectors_available,
                temporal_mode=resolved_t_mode.value,
                historical_connectors_available=bool(historical_providers),
            )
            # LIFEPILOT_AGENT_EXTERNAL_FALLBACK is deprecated: under
            # external_mode=auto, thin internal evidence already triggers
            # connectors via decide_retrieval (no second decision pass).

            retrieval_decision = policy.decision.value
            decision_meta.extend(policy.reasons)
            warnings.append(
                f"retrieval_decision:{policy.decision.value}:"
                + ",".join(policy.reasons[:6])
            )

            if policy.run_external:
                try:
                    external_items, external_outcomes, external_ms = self._retrieve_external(
                        question=question,
                        jurisdiction=jur,
                        pathway_id=pathway_id,
                        deadline_monotonic=deadline_monotonic,
                        as_of_date=(
                            (resolved_ref_date or period_end)
                            if is_historical_query
                            else None
                        ),
                        historical_only=is_historical_query,
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"external_retrieval_failed:{exc.__class__.__name__}")
                    logger.exception(
                        "agent.external_policy_error request_id=%s", request_id
                    )
                    if policy.require_verification:
                        warnings.append(
                            "external_verification_attempted_but_failed;"
                            "do_not_treat_internal_as_freshly_verified"
                        )
            elif policy.require_verification:
                warnings.append(
                    "external_verification_required_but_unavailable;"
                    "high_risk_current_figures_not_confirmed"
                )

        # Jurisdiction isolation for internal hits (defense in depth).
        internal_passages = [
            p for p in internal_passages if p.jurisdiction_code == jur
        ]
        run_external = bool(external_items or external_outcomes)

        # Normalize (curated reviewed rules first, then internal passages, then external).
        norm_started = time.perf_counter()
        evidence: list[EvidenceItem] = []
        if rule_lookup.matched_rules:
            try:
                from lifepilot.db.models import LegalRule as _LegalRule

                _rule_factory = self._session_factory_resolved()
                with _rule_factory() as _rule_session:
                    for rule in rule_lookup.matched_rules:
                        try:
                            # Re-load in this session so detached lookup rows still
                            # pass Phase 10D.4 approval-invariant validation.
                            rule_id = getattr(rule, "id", None)
                            fresh = (
                                _rule_session.get(_LegalRule, rule_id)
                                if rule_id is not None
                                else rule
                            )
                            if fresh is None:
                                continue
                            evidence.append(
                                evidence_from_legal_rule(
                                    fresh, session=_rule_session
                                )
                            )
                        except ValueError:
                            continue
            except Exception:  # noqa: BLE001
                # Fail closed: do not emit curated_rule evidence without validation.
                logger.debug(
                    "agent.curated_rule_evidence_validation_failed", exc_info=True
                )

        for passage in internal_passages:
            if not _is_substantive(passage.content_text):
                continue
            tprov = self._internal_temporal_provenance(passage)
            evidence.append(
                evidence_from_internal_passage(
                    jurisdiction=passage.jurisdiction_code,
                    official_url=passage.official_url,
                    document_id=passage.document_id,
                    document_version_id=passage.document_version_id,
                    chunk_id=passage.chunk_id,
                    content_text=passage.content_text,
                    score=passage.score,
                    heading_path=passage.heading_path,
                    source_id=passage.source_id,
                    source_name=getattr(passage, "source_name", None),
                    authority_id=getattr(passage, "authority_id", None),
                    authority_name=getattr(passage, "authority_name", None),
                    fetched_via_source_id=getattr(passage, "fetched_via_source_id", None),
                    document_version_number=getattr(passage, "version_number", None),
                    chunk_index=getattr(passage, "chunk_index", None),
                    char_start=getattr(passage, "char_start", None),
                    char_end=getattr(passage, "char_end", None),
                    content_sha256=getattr(passage, "chunk_content_sha256", None),
                    version_content_sha256=getattr(passage, "version_content_sha256", None),
                    language=getattr(passage, "language", None),
                    applicability_warning=passage.applicability_warning,
                    title=passage.title,
                    retrieved_at=passage.retrieved_at,
                    last_checked_at=passage.last_checked_at,
                    http_last_modified=passage.http_last_modified,
                    source_published_at=passage.source_published_at,
                    source_updated_at=passage.source_updated_at,
                    legal_status=passage.legal_status,
                    is_current_version=getattr(passage, "is_current_version", True),
                    temporal_provenance=tprov,
                )
            )

        for item in external_items:
            if not self._external_item_allowed(jur, item):
                continue
            passage_text = self._external_passage_text(item)
            if not _is_substantive(passage_text):
                continue
            try:
                ext_meta = item.metadata if isinstance(item.metadata, dict) else {}
                ext_tprov = self._external_temporal_provenance(item)
                evidence.append(
                    evidence_from_external_item(
                        jurisdiction=item.jurisdiction or jur,
                        official_url=item.official_url or item.canonical_url or "",
                        canonical_url=item.canonical_url,
                        provider=item.provider.value,
                        passage_text=passage_text,
                        document_id=item.document_id,
                        title=item.title,
                        source_type_label=(
                            item.source_type.value
                            if hasattr(item.source_type, "value")
                            else str(item.source_type)
                        ),
                        published_at=item.published_at,
                        updated_at=item.updated_at,
                        effective_at=item.effective_at,
                        retrieved_at=item.retrieved_at,
                        http_last_modified=ext_meta.get("http_last_modified"),
                        legal_status=item.legal_status,
                        language=item.language,
                        metadata=ext_meta,
                        temporal_provenance=ext_tprov,
                    )
                )
            except ValueError:
                continue

        # Stable de-dupe by evidence_id; preserve order (curated/internal first).
        deduped: list[EvidenceItem] = []
        seen_ids: set[str] = set()
        for ev in evidence:
            if ev.evidence_id in seen_ids:
                continue
            seen_ids.add(ev.evidence_id)
            deduped.append(ev)

        # Deterministic numeric conflict detection & temporal selection.
        conflict_notes = _numeric_conflict_warnings(deduped)
        temporal_sel = select_temporal_evidence(
            deduped,
            resolved_reference_date=resolved_ref_date,
            temporal_mode=resolved_t_mode,
            period_start=period_start,
            period_end=period_end,
            has_rule_conflicts=bool(rule_lookup.conflicting_rule_keys),
            has_numeric_conflict=bool(conflict_notes),
            cohort_clarification_required=rule_lookup.cohort_clarification_required,
            period_clarification_required=rule_lookup.period_clarification_required,
            unreviewed_rules_excluded_count=rule_lookup.unreviewed_excluded_count,
            unknown_date_rules_excluded_count=rule_lookup.unknown_date_excluded_count,
        )
        evidence = temporal_sel.kept_evidence[: cfg.max_evidence_items]
        warnings.extend(temporal_sel.warnings)
        normalize_ms = (time.perf_counter() - norm_started) * 1000

        # Provider limitation + partial failure warnings.
        for outcome in external_outcomes:
            note = PROVIDER_LIMITATION_NOTES.get(outcome.provider.value)
            if note and outcome.status == ExternalRetrievalStatus.OK and outcome.items:
                warnings.append(f"{outcome.provider.value}: {note}")
            if outcome.status not in (
                ExternalRetrievalStatus.OK,
                ExternalRetrievalStatus.NO_RESULTS,
            ):
                warnings.append(
                    f"partial_source:{outcome.provider.value}:{outcome.status.value}"
                    + (f":{outcome.error_detail}" if outcome.error_detail else "")
                )

        external_was_attempted = (
            (resolved_mode == ExternalMode.ON and (not is_historical_query or bool(historical_providers)))
            or run_external
            or any(
                w.startswith("retrieval_decision:external")
                or "external_verification" in w
                for w in warnings
            )
        )
        if (
            resolved_mode != ExternalMode.OFF
            and external_was_attempted
            and not explicit_providers_for_jurisdiction(jur)
        ):
            warnings.append(
                f"No mapped external providers for jurisdiction {jur}; "
                "external retrieval skipped."
            )

        # Re-check numeric conflicts on retained evidence and emit notes.
        conflict_notes = _numeric_conflict_warnings(evidence) or conflict_notes
        warnings.extend(conflict_notes)

        # Conservative effective-date scan (never invent legal effective dates).
        date_notes = reconcile_effective_dates(
            evidence, reference_date=resolved_ref_date
        )
        warnings.extend(date_notes.warnings)

        status, missing = self._assess_sufficiency(
            evidence=evidence,
            internal_status=internal_status,
            include_external=external_was_attempted,
            external_outcomes=external_outcomes,
            errors=errors,
            jurisdiction=jur,
        )
        missing.extend(intent_missing)
        missing.extend(temporal_sel.missing_information)
        missing.extend(date_notes.missing_information)
        if conflict_notes:
            missing.append("resolved_current_numerical_requirement")
            warnings.append(
                "numeric_conflict_unresolved:omit_or_qualify_definitive_figures"
            )

        # Preserve usable evidence: demote leg failures to warnings rather than
        # flipping a successful evidence bundle into execution_failure.
        if status == AgentStatus.SUCCESS and errors:
            for err in errors:
                warnings.append(f"partial_failure:{err}")
            errors = []

        logger.info(
            "agent.evidence request_id=%s jur=%s decision=%s tmode=%s toutcome=%s "
            "internal=%s evidence=%s status=%s reasons=%s",
            request_id,
            jur,
            retrieval_decision,
            resolved_t_mode.value,
            temporal_sel.temporal_outcome.value,
            internal_status,
            len(evidence),
            status.value,
            decision_meta[:8],
        )
        profile = {
            "normalize_ms": round(normalize_ms, 3),
            "internal_ms": round(internal_ms, 3),
            "external_ms": round(external_ms, 3),
            **getattr(self, "_last_internal_profile", {}),
        }
        return OrchestrationResult(
            evidence=evidence,
            status=status,
            warnings=list(dict.fromkeys(warnings)),
            errors=errors,
            missing_information=list(dict.fromkeys(missing)),
            internal_ms=round(internal_ms, 3),
            external_ms=round(external_ms, 3),
            normalize_ms=round(normalize_ms, 3),
            internal_status=internal_status,
            external_provider_outcomes=[
                {
                    "provider": o.provider.value,
                    "status": o.status.value,
                    "item_count": len(o.items),
                    "error_detail": o.error_detail,
                }
                for o in external_outcomes
            ],
            profile_ms=profile,
            retrieval_decision=retrieval_decision,
            retrieval_decision_reasons=list(decision_meta),
            resolved_reference_date=(
                resolved_ref_date.isoformat() if resolved_ref_date else None
            ),
            temporal_mode=resolved_t_mode.value,
            temporal_outcome=temporal_sel.temporal_outcome.value,
            excluded_evidence=temporal_sel.excluded_items,
        )

    def _assess_sufficiency(
        self,
        *,
        evidence: Sequence[EvidenceItem],
        internal_status: str,
        include_external: bool,
        external_outcomes: Sequence[ProviderOutcome],
        errors: Sequence[str],
        jurisdiction: str,
    ) -> tuple[AgentStatus, list[str]]:
        missing: list[str] = []
        has_evidence = any(_is_substantive(e.passage_text) for e in evidence)
        hard_errors = list(errors)
        external_hard_fail = include_external and bool(external_outcomes) and all(
            o.status
            in {
                ExternalRetrievalStatus.TRANSPORT_ERROR,
                ExternalRetrievalStatus.TIMEOUT,
                ExternalRetrievalStatus.UNAVAILABLE,
                ExternalRetrievalStatus.BLOCKED,
            }
            for o in external_outcomes
        )
        internal_failed = internal_status == "error"

        if has_evidence:
            # Preserve usable evidence even if some providers failed.
            return AgentStatus.SUCCESS, missing

        if internal_failed and (not include_external or external_hard_fail):
            return AgentStatus.EXECUTION_FAILURE, ["retrieved_evidence"]

        if include_external and not explicit_providers_for_jurisdiction(jurisdiction):
            # No external mapping and no internal hits.
            if internal_status in {"ok_empty", "no_results", "ok", "unsupported_filter"}:
                return AgentStatus.INSUFFICIENT_EVIDENCE, ["retrieved_evidence"]

        if hard_errors and internal_failed and (not include_external or external_hard_fail):
            return AgentStatus.EXECUTION_FAILURE, ["retrieved_evidence"]

        missing.append("retrieved_evidence")
        return AgentStatus.INSUFFICIENT_EVIDENCE, missing

    def _lookup_version_retrieved_at(
        self, version_ids: list[int | None]
    ) -> dict[int, datetime]:
        """Map document_version_id → retrieved_at for freshness assessment."""
        ids = sorted({int(v) for v in version_ids if v is not None})
        if not ids:
            return {}
        try:
            from lifepilot.db.models import DocumentVersion

            factory = self._session_factory_resolved()
            with factory() as session:
                rows = (
                    session.query(
                        DocumentVersion.id,
                        DocumentVersion.retrieved_at,
                        DocumentVersion.last_checked_at,
                    )
                    .filter(DocumentVersion.id.in_(ids))
                    .all()
                )
            out: dict[int, datetime] = {}
            for vid, retrieved_at, last_checked_at in rows:
                # Prefer last_checked_at for freshness age; never erase retrieved_at.
                anchor = last_checked_at or retrieved_at
                if anchor is not None:
                    out[int(vid)] = anchor
            return out
        except Exception:  # noqa: BLE001
            logger.debug(
                "agent.version_retrieved_at_lookup_failed", exc_info=True
            )
            return {}

    def _session_factory_resolved(self) -> sessionmaker[Session]:
        if self._session_factory is not None:
            return self._session_factory
        from lifepilot.db.session import get_session_factory

        return get_session_factory()

    def _get_router(self) -> ExternalKnowledgeRouter:
        if self._external_router is not None:
            return self._external_router
        timeout = float(self.config.external_timeout_seconds)
        ttl = float(self.config.external_http_cache_ttl_seconds or 0.0)
        cache = None
        enable_disk = False
        if ttl > 0:
            from lifepilot.external.cache import ResponseCache
            from lifepilot.external.router import DEFAULT_CACHE_DIR

            enable_disk = bool(self.config.external_http_disk_cache)
            cache = ResponseCache(
                default_ttl_seconds=ttl,
                disk_dir=DEFAULT_CACHE_DIR if enable_disk else None,
            )
        http = ExternalHttpClient(
            config=HttpClientConfig(
                timeout_seconds=timeout,
                max_retries=2,
            ),
            cache=cache,
            allowed_hosts=ALL_ALLOWED_HOSTS,
        )
        # Attach the same cache object the HTTP client uses so connector
        # use_cache=True hits actually work (router-only cache was previously unused).
        self._external_router = ExternalKnowledgeRouter(
            http=http,
            cache=cache,
            enable_disk_cache=enable_disk,
        )
        self._owns_router = True
        return self._external_router

    def _retrieve_curated_rules(
        self,
        *,
        jurisdiction: str,
        as_of_date: date | None,
        pathway_id: str | None,
        user_circumstances: dict[str, Any] | None,
        period_start: date | None = None,
        period_end: date | None = None,
    ) -> LegalRuleLookupResult:
        """Query reviewed LegalRule records without failing mocked/unmigrated setups."""
        if self._session_factory is None and "_retrieve_internal" in self.__dict__:
            return LegalRuleLookupResult(
                status="empty",
                as_of_date=as_of_date,
                jurisdiction=jurisdiction,
                pathway_id=pathway_id,
            )
        try:
            factory = self._session_factory_resolved()
            with factory() as session:
                return query_reviewed_legal_rules(
                    session,
                    jurisdiction=jurisdiction,
                    as_of_date=as_of_date,
                    pathway_id=pathway_id,
                    user_circumstances=user_circumstances,
                    period_start=period_start,
                    period_end=period_end,
                )
        except Exception:  # noqa: BLE001
            logger.debug("agent.curated_rule_lookup_skipped", exc_info=True)
            return LegalRuleLookupResult(
                status="empty",
                as_of_date=as_of_date,
                jurisdiction=jurisdiction,
                pathway_id=pathway_id,
            )

    def _retrieve_internal(
        self,
        *,
        question: str,
        jurisdiction: str,
        mode: RetrievalMode,
        top_k: int,
        deadline_monotonic: float | None,
        include_historical: bool = False,
        as_of_date: date | None = None,
        temporal_mode: str = "current",
    ) -> tuple[list[RetrievedPassage], str, float]:
        remaining = _deadline_remaining(deadline_monotonic)
        if remaining is not None and remaining <= 0:
            raise TimeoutError("internal retrieval deadline exceeded")

        started = time.perf_counter()
        embedder = self._resolve_embedder()
        t_factory = time.perf_counter()
        factory = self._session_factory_resolved()
        factory_ms = (time.perf_counter() - t_factory) * 1000
        t_session = time.perf_counter()
        with factory() as session:
            session_ms = (time.perf_counter() - t_session) * 1000
            t_ret_ctor = time.perf_counter()
            if self._internal_retriever_factory is not None:
                retriever = self._internal_retriever_factory(
                    session, embedder=embedder
                )
            else:
                retriever = InternalKnowledgeRetriever(
                    session, embedder=embedder
                )
            retriever_ctor_ms = (time.perf_counter() - t_ret_ctor) * 1000
            # Never pass pathway_id — hard pathway filters remain unsupported.
            t_retrieve = time.perf_counter()
            result = retriever.retrieve(
                InternalQuery(
                    query=question,
                    jurisdiction=jurisdiction,
                    mode=mode,
                    top_k=top_k,
                    pathway_id=None,
                    requirement=None,
                    diversify_documents=True,
                    document_aware_passages=True,
                    expand_adjacent_chunks=True,
                    as_of_date=as_of_date,
                    include_historical=include_historical,
                    temporal_mode=temporal_mode,
                )
            )
            retrieve_ms = (time.perf_counter() - t_retrieve) * 1000
        elapsed = (time.perf_counter() - started) * 1000
        # Stash lightweight component timings on the orchestrator for gather().
        self._last_internal_profile = {
            "session_factory_ms": round(factory_ms, 3),
            "session_acquire_ms": round(session_ms, 3),
            "retriever_ctor_ms": round(retriever_ctor_ms, 3),
            "retrieve_ms": round(retrieve_ms, 3),
            "retriever_latency_ms": round(float(result.latency_ms), 3),
        }
        if result.status == InternalRetrievalStatus.OK and result.passages:
            return result.passages, "ok", elapsed
        if result.status == InternalRetrievalStatus.NO_RESULTS:
            return [], "no_results", elapsed
        if result.status == InternalRetrievalStatus.UNSUPPORTED_FILTER:
            return [], "unsupported_filter", elapsed
        if result.status == InternalRetrievalStatus.INVALID_REQUEST:
            return [], "invalid_request", elapsed
        return list(result.passages or []), "ok_empty", elapsed

    def _resolve_embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = get_shared_embedder()
        return self._embedder

    def _retrieve_external(
        self,
        *,
        question: str,
        jurisdiction: str,
        pathway_id: str | None,
        deadline_monotonic: float | None,
        as_of_date: date | None = None,
        historical_only: bool = False,
    ) -> tuple[list[ExternalEvidence], list[ProviderOutcome], float]:
        remaining = _deadline_remaining(deadline_monotonic)
        if remaining is not None and remaining <= 0:
            raise TimeoutError("external retrieval deadline exceeded")

        started = time.perf_counter()
        jur = resolve_jurisdiction(jurisdiction, pathway_id) or jurisdiction
        # Explicit mapping only — never fall back to every connector.
        selected = explicit_providers_for_jurisdiction(jur)
        # Keep EU connectors out of non-EU national requests.
        if jur not in EU_RELATED_JURISDICTIONS:
            selected = [p for p in selected if p not in EU_PROVIDER_IDS]
        if historical_only:
            selected = [p for p in selected if p in _HISTORICAL_EXTERNAL_PROVIDERS]
        # Deduplicate provider ids (prevent duplicate connector calls).
        seen: set[ProviderId] = set()
        unique_selected: list[ProviderId] = []
        for pid in selected:
            if pid in seen:
                continue
            seen.add(pid)
            unique_selected.append(pid)
        selected = unique_selected

        if not selected:
            return [], [], (time.perf_counter() - started) * 1000

        router = self._get_router()
        query = ExternalQuery(
            query=question,
            jurisdiction=jur,
            pathway_id=pathway_id,
            published_after=None,
            published_before=(
                as_of_date.isoformat()
                if historical_only and as_of_date is not None
                else None
            ),
            max_results_per_provider=min(5, self.config.max_evidence_items),
        )

        outcomes: list[ProviderOutcome] = []
        max_workers = max(1, min(self.config.max_external_concurrency, len(selected)))
        # Bound wall time for the provider pool.
        pool_timeout = _deadline_remaining(deadline_monotonic)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self._run_provider, router, pid, query): pid
                for pid in selected
            }
            try:
                for fut in as_completed(futures, timeout=pool_timeout):
                    pid = futures[fut]
                    try:
                        outcomes.append(fut.result())
                    except Exception as exc:  # noqa: BLE001
                        outcomes.append(
                            ProviderOutcome(
                                provider=pid,
                                status=ExternalRetrievalStatus.TRANSPORT_ERROR,
                                error_detail=f"Connector raised: {exc}",
                            )
                        )
            except TimeoutError:
                for fut, pid in futures.items():
                    if fut.done():
                        continue
                    fut.cancel()
                    outcomes.append(
                        ProviderOutcome(
                            provider=pid,
                            status=ExternalRetrievalStatus.TIMEOUT,
                            error_detail="Provider timed out under request deadline",
                        )
                    )

        items = dedupe_evidence(item for o in outcomes for item in o.items)
        elapsed = (time.perf_counter() - started) * 1000
        return items, outcomes, elapsed

    def _run_provider(
        self,
        router: ExternalKnowledgeRouter,
        provider_id: ProviderId,
        query: ExternalQuery,
    ) -> ProviderOutcome:
        connector = router._connectors[provider_id]
        if (
            not connector.supports(query)
            and query.jurisdiction
            and provider_id not in (query.providers or ())
        ):
            return ProviderOutcome(
                provider=provider_id,
                status=ExternalRetrievalStatus.NO_RESULTS,
                error_detail=(
                    f"Provider not applicable for jurisdiction {query.jurisdiction}"
                ),
                capabilities=list(connector.capabilities),
            )
        return connector.search(query)

    def _external_item_allowed(self, request_jur: str, item: ExternalEvidence) -> bool:
        item_jur = (item.jurisdiction or "").strip().upper()
        if item.provider in EU_PROVIDER_IDS and request_jur not in EU_RELATED_JURISDICTIONS:
            return False
        if item_jur == "EU" and request_jur not in EU_RELATED_JURISDICTIONS:
            return False
        # Prevent obvious cross-jurisdiction leakage (e.g. CA item under GB).
        if item_jur and item_jur not in {request_jur, "EU"}:
            if not (
                request_jur in EU_RELATED_JURISDICTIONS and item_jur in EU_RELATED_JURISDICTIONS
            ):
                return False
        return True

    @staticmethod
    def _external_passage_text(item: ExternalEvidence) -> str:
        if item.passages:
            return "\n".join(p for p in item.passages if p and str(p).strip())
        # Fall back to title only when passages absent — may fail substantive check.
        return (item.title or "").strip()

    @staticmethod
    def _internal_temporal_provenance(passage: RetrievedPassage) -> dict[str, Any]:
        import json as _json

        prov: dict[str, Any] = {
            "origin": "internal_rag",
            "effective_at_known": False,
        }
        raw_notes = getattr(passage, "temporal_notes", None)
        if raw_notes and isinstance(raw_notes, str):
            try:
                parsed = _json.loads(raw_notes)
                if isinstance(parsed, dict):
                    if parsed.get("extraction_conflicts"):
                        prov["conflicts"] = parsed["extraction_conflicts"]
                        prov["has_conflicts"] = True
                    if parsed.get("pdf_file_created_at") or parsed.get("pdf_file_modified_at"):
                        prov["is_file_metadata"] = True
                        prov["pdf_file_created_at"] = parsed.get("pdf_file_created_at")
                        prov["pdf_file_modified_at"] = parsed.get("pdf_file_modified_at")
                    if parsed.get("published_provenance"):
                        prov["published_provenance"] = parsed["published_provenance"]
                    if parsed.get("updated_provenance"):
                        prov["updated_provenance"] = parsed["updated_provenance"]
            except _json.JSONDecodeError:
                pass
        return prov

    @staticmethod
    def _external_temporal_provenance(item: ExternalEvidence) -> dict[str, Any]:
        ext_meta = item.metadata if isinstance(item.metadata, dict) else {}
        prov: dict[str, Any] = {
            "origin": "external_api",
            "provider": item.provider.value,
            "effective_at_is_connector_assertion": bool(item.effective_at),
            "legal_status_is_connector_assertion": bool(item.legal_status),
        }
        nested = ext_meta.get("temporal_provenance")
        if isinstance(nested, dict):
            if nested.get("conflicts"):
                prov["conflicts"] = nested["conflicts"]
                prov["has_conflicts"] = True
            if nested.get("published_provenance"):
                prov["published_provenance"] = nested["published_provenance"]
            if nested.get("updated_provenance"):
                prov["updated_provenance"] = nested["updated_provenance"]
        return prov


_orchestrator_override: EvidenceOrchestrator | None = None
_default_orchestrator: EvidenceOrchestrator | None = None


def get_evidence_orchestrator(
    config: AgentConfig | None = None,
) -> EvidenceOrchestrator:
    """Return the process default orchestrator (reuses embedder/router state)."""
    global _default_orchestrator
    if _orchestrator_override is not None:
        return _orchestrator_override
    if _default_orchestrator is None:
        _default_orchestrator = EvidenceOrchestrator(
            config=config or get_agent_config()
        )
    return _default_orchestrator


def set_evidence_orchestrator_override(
    orchestrator: EvidenceOrchestrator | None,
) -> None:
    """Tests only: inject a fake orchestrator for offline determinism."""
    global _orchestrator_override
    _orchestrator_override = orchestrator


def reset_evidence_orchestrator_override() -> None:
    set_evidence_orchestrator_override(None)


def reset_default_evidence_orchestrator() -> None:
    """Drop the process default orchestrator (tests only)."""
    global _default_orchestrator
    if _default_orchestrator is not None:
        _default_orchestrator.close()
    _default_orchestrator = None
