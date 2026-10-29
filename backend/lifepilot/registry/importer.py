"""Idempotent importer for curated official government sources.

Imports registry metadata only (jurisdictions, authorities, sources).
Does not create documents, download files, or crawl.

``review_status`` is never changed by import or HTTP verification. New sources
are created as ``candidate``. Explicit approval uses
``lifepilot.registry.review.set_source_review_status``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.db.models import Authority, Jurisdiction, Source
from lifepilot.registry.seeds import (
    AUTHORITIES,
    JURISDICTIONS,
    SOURCES,
    AuthoritySeed,
    JurisdictionSeed,
    SourceSeed,
)
from lifepilot.registry.urls import (
    InvalidRegistryUrlError,
    path_prefix_pattern,
    patterns_expand_scope,
    validate_seed_and_patterns,
)
from lifepilot.registry.verify import VerificationResult, verify_seed_url

# Reviewed statuses: crawl boundaries must not change on import.
_REVIEWED_STATUSES = frozenset({"approved", "rejected"})


@dataclass
class SourceImportResult:
    key: str
    seed_url: str
    seed_url_normalized: str
    review_status: str
    action: str  # created | updated | unchanged | skipped_invalid
    verification: VerificationResult | None = None
    error: str | None = None
    warning: str | None = None


@dataclass
class ImportReport:
    jurisdictions_created: int = 0
    jurisdictions_updated: int = 0
    authorities_created: int = 0
    authorities_updated: int = 0
    sources: list[SourceImportResult] = field(default_factory=list)

    @property
    def sources_created(self) -> int:
        return sum(1 for s in self.sources if s.action == "created")

    @property
    def sources_updated(self) -> int:
        return sum(1 for s in self.sources if s.action == "updated")

    @property
    def sources_unchanged(self) -> int:
        return sum(1 for s in self.sources if s.action == "unchanged")

    @property
    def approved_count(self) -> int:
        return sum(1 for s in self.sources if s.review_status == "approved")

    @property
    def candidate_count(self) -> int:
        return sum(1 for s in self.sources if s.review_status == "candidate")

    @property
    def rejected_count(self) -> int:
        return sum(1 for s in self.sources if s.review_status == "rejected")


def _build_notes(seed: SourceSeed) -> str:
    """Static registry notes only (not live verification output)."""
    topics = ", ".join(seed.topics)
    parts = [
        f"seed_key={seed.key}",
        f"topics={topics}",
        (
            "crawl_permission=unchecked "
            "(verify robots.txt, access terms, and rate limits before crawling)"
        ),
    ]
    if seed.extra_notes:
        parts.append(seed.extra_notes)
    return " | ".join(parts)


def resolve_patterns_for_import(
    *,
    review_status: str,
    existing_patterns: list[str] | None,
    proposed_patterns: list[str],
) -> tuple[list[str], str | None]:
    """Choose patterns to store; protect reviewed crawl scopes from silent changes.

    Approved/rejected sources keep their existing boundaries. Candidate sources
    may take the proposed seed patterns. Returns ``(patterns, warning_or_none)``.
    """
    existing = list(existing_patterns or [])
    if review_status not in _REVIEWED_STATUSES:
        return proposed_patterns, None

    if existing == proposed_patterns:
        return existing, None

    if patterns_expand_scope(existing, proposed_patterns):
        return existing, (
            "refused to expand reviewed crawl scope; "
            "existing allowed_url_patterns preserved"
        )
    return existing, (
        "preserved reviewed crawl scope; "
        "seed pattern changes ignored until explicitly approved"
    )


def _upsert_jurisdictions(
    session: Session,
    report: ImportReport,
    jurisdictions: tuple[JurisdictionSeed, ...] = JURISDICTIONS,
) -> dict[str, Jurisdiction]:
    by_code: dict[str, Jurisdiction] = {}
    for seed in jurisdictions:
        existing = session.scalar(
            select(Jurisdiction).where(Jurisdiction.code == seed.code)
        )
        if existing is None:
            row = Jurisdiction(
                code=seed.code,
                name=seed.name,
                jurisdiction_type=seed.jurisdiction_type,
                country_code=seed.country_code,
            )
            session.add(row)
            session.flush()
            by_code[seed.code] = row
            report.jurisdictions_created += 1
            continue

        changed = False
        if existing.name != seed.name:
            existing.name = seed.name
            changed = True
        if existing.jurisdiction_type != seed.jurisdiction_type:
            existing.jurisdiction_type = seed.jurisdiction_type
            changed = True
        if existing.country_code != seed.country_code:
            existing.country_code = seed.country_code
            changed = True
        if changed:
            report.jurisdictions_updated += 1
        by_code[seed.code] = existing
    return by_code


def _upsert_authorities(
    session: Session,
    jurisdictions: dict[str, Jurisdiction],
    report: ImportReport,
    authorities: tuple[AuthoritySeed, ...] = AUTHORITIES,
) -> dict[str, Authority]:
    by_key: dict[str, Authority] = {}
    for seed in authorities:
        jurisdiction = jurisdictions[seed.jurisdiction_code]
        existing = session.scalar(
            select(Authority).where(
                Authority.jurisdiction_id == jurisdiction.id,
                Authority.name == seed.name,
            )
        )
        if existing is None:
            row = Authority(
                jurisdiction_id=jurisdiction.id,
                name=seed.name,
                short_name=seed.short_name,
                website_url=seed.website_url,
            )
            session.add(row)
            session.flush()
            by_key[seed.key] = row
            report.authorities_created += 1
            continue

        changed = False
        if existing.short_name != seed.short_name:
            existing.short_name = seed.short_name
            changed = True
        if existing.website_url != seed.website_url:
            existing.website_url = seed.website_url
            changed = True
        if changed:
            report.authorities_updated += 1
        by_key[seed.key] = existing
    return by_key


def _upsert_source(
    session: Session,
    seed: SourceSeed,
    *,
    jurisdiction: Jurisdiction,
    authority: Authority,
    verify: bool,
    http_client=None,
) -> SourceImportResult:
    try:
        raw_patterns = [path_prefix_pattern(seed.seed_url), *seed.extra_allowed_patterns]
        normalized, patterns = validate_seed_and_patterns(
            seed.seed_url,
            raw_patterns,
            require_https=True,
        )
    except InvalidRegistryUrlError as exc:
        return SourceImportResult(
            key=seed.key,
            seed_url=seed.seed_url,
            seed_url_normalized="",
            review_status="candidate",
            action="skipped_invalid",
            error=str(exc),
        )

    existing = session.scalar(
        select(Source).where(Source.seed_url_normalized == normalized)
    )

    verification: VerificationResult | None = None
    if verify:
        verification = verify_seed_url(
            seed.seed_url,
            seed.expected_hosts,
            client=http_client,
        )

    notes = _build_notes(seed)

    # New sources always start as candidate. Never auto-approve from HTTP.
    if existing is None:
        row = Source(
            authority_id=authority.id,
            jurisdiction_id=jurisdiction.id,
            name=seed.name,
            seed_url=seed.seed_url,
            seed_url_normalized=normalized,
            allowed_url_patterns=patterns,
            review_status="candidate",
            default_language=seed.language,
            notes=notes,
        )
        session.add(row)
        session.flush()
        return SourceImportResult(
            key=seed.key,
            seed_url=seed.seed_url,
            seed_url_normalized=normalized,
            review_status=row.review_status,
            action="created",
            verification=verification,
        )

    # Preserve explicit review decisions (approved / candidate / rejected).
    # Never silently expand (or otherwise rewrite) reviewed crawl scopes.
    patterns_to_store, scope_warning = resolve_patterns_for_import(
        review_status=existing.review_status,
        existing_patterns=list(existing.allowed_url_patterns or []),
        proposed_patterns=patterns,
    )

    changed = False
    desired = {
        "authority_id": authority.id,
        "jurisdiction_id": jurisdiction.id,
        "name": seed.name,
        "seed_url": seed.seed_url,
        "allowed_url_patterns": patterns_to_store,
        "default_language": seed.language,
        "notes": notes,
    }
    for attr, value in desired.items():
        if getattr(existing, attr) != value:
            setattr(existing, attr, value)
            changed = True

    return SourceImportResult(
        key=seed.key,
        seed_url=seed.seed_url,
        seed_url_normalized=normalized,
        review_status=existing.review_status,
        action="updated" if changed else "unchanged",
        verification=verification,
        warning=scope_warning,
    )


def import_seed_sources(
    session: Session,
    *,
    verify: bool = True,
    http_client=None,
    jurisdictions: tuple[JurisdictionSeed, ...] | None = None,
    authorities: tuple[AuthoritySeed, ...] | None = None,
    sources: tuple[SourceSeed, ...] | None = None,
) -> ImportReport:
    """Upsert curated seeds into the registry. Safe to run repeatedly.

    Does not modify ``review_status`` for existing rows and never auto-approves.
    Optional seed tuples enable configuration-driven imports without changing
    Python ingestion logic.
    """
    jur_seeds = jurisdictions if jurisdictions is not None else JURISDICTIONS
    auth_seeds = authorities if authorities is not None else AUTHORITIES
    source_seeds = sources if sources is not None else SOURCES

    report = ImportReport()
    jur_rows = _upsert_jurisdictions(session, report, jur_seeds)
    auth_rows = _upsert_authorities(session, jur_rows, report, auth_seeds)

    for seed in source_seeds:
        if seed.jurisdiction_code not in jur_rows:
            report.sources.append(
                SourceImportResult(
                    key=seed.key,
                    seed_url=seed.seed_url,
                    seed_url_normalized="",
                    review_status="candidate",
                    action="skipped_invalid",
                    error=f"unknown jurisdiction_code={seed.jurisdiction_code!r}",
                )
            )
            continue
        if seed.authority_key not in auth_rows:
            # Authority may already exist in DB from a prior import with the same name.
            # Resolve by jurisdiction + authority seed name if key missing from this batch.
            report.sources.append(
                SourceImportResult(
                    key=seed.key,
                    seed_url=seed.seed_url,
                    seed_url_normalized="",
                    review_status="candidate",
                    action="skipped_invalid",
                    error=f"unknown authority_key={seed.authority_key!r} in this import set",
                )
            )
            continue
        result = _upsert_source(
            session,
            seed,
            jurisdiction=jur_rows[seed.jurisdiction_code],
            authority=auth_rows[seed.authority_key],
            verify=verify,
            http_client=http_client,
        )
        report.sources.append(result)

    session.flush()
    return report


def import_registry_config(
    session: Session,
    *,
    jurisdictions: tuple[JurisdictionSeed, ...],
    authorities: tuple[AuthoritySeed, ...],
    sources: tuple[SourceSeed, ...],
    verify: bool = True,
    http_client=None,
) -> ImportReport:
    """Import a configuration-driven registry batch (always candidate for new rows)."""
    return import_seed_sources(
        session,
        verify=verify,
        http_client=http_client,
        jurisdictions=jurisdictions,
        authorities=authorities,
        sources=sources,
    )


def sync_report_review_statuses(session: Session, report: ImportReport) -> ImportReport:
    """Refresh report statuses from DB (e.g. after explicit ``--set-status``)."""
    for item in report.sources:
        if not item.seed_url_normalized:
            continue
        row = session.scalar(
            select(Source).where(
                Source.seed_url_normalized == item.seed_url_normalized
            )
        )
        if row is not None:
            item.review_status = row.review_status
    return report
