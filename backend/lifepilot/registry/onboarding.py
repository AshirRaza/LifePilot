"""Candidate source verification and approval-manifest generation.

Verification facts are separated from approval recommendations. Successful HTTP
reachability never auto-approves a source.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import certifi
import httpx

from lifepilot.crawl.policy import fetcher_policy
from lifepilot.crawl.robots import check_robots
from lifepilot.registry.seeds import SourceSeed
from lifepilot.registry.verify import VerificationResult, verify_seed_url

DEFAULT_ONBOARDING_UA = (
    "LifePilotSourceOnboarding/0.1 "
    "(+https://github.com/AshirRaza/LifePilot; "
    "official-source verification only; not a crawler)"
)


@dataclass
class SourceVerificationRecord:
    key: str
    country_code: str
    authority_key: str
    authority_name: str
    source_name: str
    seed_url: str
    topics: list[str]
    language: str
    proposed_url_scope: list[str]
    expected_hosts: list[str]
    facts: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    recommendation: str = "further_review"
    recommendation_rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApprovalManifest:
    generated_at: str
    config_path: str | None
    notes: list[str]
    sources: list[SourceVerificationRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "config_path": self.config_path,
            "notes": self.notes,
            "sources": [s.to_dict() for s in self.sources],
        }


def _content_type_kind(content_type: str | None) -> str:
    if not content_type:
        return "unknown"
    ct = content_type.split(";")[0].strip().lower()
    if "html" in ct:
        return "html"
    if ct == "application/pdf" or ct.endswith("/pdf"):
        return "pdf"
    if (
        "officedocument.wordprocessingml" in ct
        or ct.endswith("msword")
        or "docx" in ct
    ):
        return "docx"
    return ct or "unknown"


def _looks_like_bot_challenge(text_sample: str, title_hint: str | None = None) -> bool:
    blob = f"{title_hint or ''}\n{text_sample}".lower()
    markers = (
        "radware",
        "access denied",
        "captcha",
        "cf-browser-verification",
        "attention required",
        "bot detection",
        "please enable cookies",
        "request unsuccessful",
    )
    return any(m in blob for m in markers)


def deepen_source_verification(
    seed: SourceSeed,
    *,
    authority_name: str,
    path_prefix_patterns: list[str],
    http_client: httpx.Client | None = None,
    base_verification: VerificationResult | None = None,
) -> SourceVerificationRecord:
    """Collect verification facts for one candidate seed (no approval mutation)."""
    owns = http_client is None
    client = http_client or httpx.Client(
        timeout=30.0,
        follow_redirects=True,
        verify=certifi.where(),
        headers={
            "User-Agent": DEFAULT_ONBOARDING_UA,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        },
    )
    record = SourceVerificationRecord(
        key=seed.key,
        country_code=seed.jurisdiction_code,
        authority_key=seed.authority_key,
        authority_name=authority_name,
        source_name=seed.name,
        seed_url=seed.seed_url,
        topics=list(seed.topics),
        language=seed.language,
        proposed_url_scope=list(path_prefix_patterns),
        expected_hosts=list(seed.expected_hosts),
    )
    facts: dict[str, Any] = {
        "official_domain_expected": list(seed.expected_hosts),
        "verification_is_not_approval": True,
    }
    warnings: list[str] = []

    try:
        verification = base_verification or verify_seed_url(
            seed.seed_url, seed.expected_hosts, client=client
        )
        facts["connectivity_ok"] = verification.connectivity_ok
        facts["landed_on_expected_host"] = verification.landed_on_expected_host
        facts["http_status"] = verification.status_code
        facts["final_url"] = verification.final_url
        facts["final_host"] = verification.final_host
        facts["redirected"] = verification.redirected
        if verification.error:
            facts["connectivity_error"] = verification.error

        policy = fetcher_policy()
        robots = check_robots(seed.seed_url, policy, client=client)
        facts["robots"] = {
            "fetched": robots.fetched,
            "status_code": robots.status_code,
            "can_fetch": robots.can_fetch,
            "crawl_delay_seconds": robots.crawl_delay_seconds,
            "error": robots.error,
            "robots_url": robots.robots_url,
        }
        if robots.fetched and robots.can_fetch is False:
            warnings.append("robots.txt disallows the configured crawler user-agent")
        if not robots.fetched:
            warnings.append("robots.txt could not be fetched; crawl permission unchecked")

        content_kind = "unknown"
        bot_protection_suspected = False
        title_hint = None
        if verification.connectivity_ok and verification.final_url:
            try:
                with client.stream("GET", seed.seed_url) as response:
                    content_kind = _content_type_kind(
                        response.headers.get("content-type")
                    )
                    facts["response_content_type"] = response.headers.get("content-type")
                    # Bounded sample only — not document ingestion.
                    chunk = b""
                    for part in response.iter_bytes():
                        chunk += part
                        if len(chunk) >= 8192:
                            break
                    response.close()
                sample = chunk.decode("utf-8", errors="replace")
                if "<title" in sample.lower():
                    start = sample.lower().find("<title")
                    start = sample.find(">", start) + 1
                    end = sample.lower().find("</title>", start)
                    if start > 0 and end > start:
                        title_hint = sample[start:end].strip()[:200]
                bot_protection_suspected = _looks_like_bot_challenge(sample, title_hint)
            except httpx.HTTPError as exc:
                warnings.append(f"content probe failed: {exc}")
        facts["content_kind_observed"] = content_kind
        facts["title_hint"] = title_hint
        facts["bot_protection_suspected"] = bot_protection_suspected
        if bot_protection_suspected:
            warnings.append(
                "response sample suggests bot-protection / challenge page"
            )

        english_available = seed.language.lower().startswith("en")
        facts["declared_language"] = seed.language
        facts["english_language_declared"] = english_available
        if not english_available:
            warnings.append(
                "primary language is not English; confirm official EN page before approval"
            )

        # Recommendation (not a fact / not an approval).
        if not verification.connectivity_ok:
            recommendation = "further_review"
            rationale = "seed URL was not reliably reachable"
        elif not verification.landed_on_expected_host:
            recommendation = "reject_or_revise_url"
            rationale = "final host left the expected official domain set"
        elif bot_protection_suspected:
            recommendation = "further_review"
            rationale = (
                "reachable on expected host but bot-protection suspected; "
                "keep as candidate until crawl path is validated"
            )
        elif robots.fetched and robots.can_fetch is False:
            recommendation = "further_review"
            rationale = "robots.txt disallows crawl agent; human review required"
        else:
            recommendation = "eligible_for_manual_approval_review"
            rationale = (
                "reachability and expected host look sound; "
                "manual approval still required before any ingestion"
            )

        record.facts = facts
        record.warnings = warnings
        record.recommendation = recommendation
        record.recommendation_rationale = rationale
        return record
    finally:
        if owns:
            client.close()


def write_approval_manifest(manifest: ApprovalManifest, path: Path | str) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return out


def build_approval_manifest(
    *,
    sources: list[tuple[SourceSeed, str, list[str]]],
    config_path: str | None,
    http_client: httpx.Client | None = None,
    verify: bool = True,
) -> ApprovalManifest:
    """``sources`` items are ``(seed, authority_name, proposed_patterns)``."""
    manifest = ApprovalManifest(
        generated_at=datetime.now(timezone.utc).isoformat(),
        config_path=config_path,
        notes=[
            "All listed sources must remain candidates until explicitly approved.",
            "Verification facts are not approval decisions.",
            "Successful HTTP responses do not imply crawl permission or content authenticity.",
            "EU / member-state sources are jurisdiction-scoped; do not treat EU pages as "
            "belonging to every EU country.",
        ],
    )
    if not verify:
        for seed, authority_name, patterns in sources:
            record = SourceVerificationRecord(
                key=seed.key,
                country_code=seed.jurisdiction_code,
                authority_key=seed.authority_key,
                authority_name=authority_name,
                source_name=seed.name,
                seed_url=seed.seed_url,
                topics=list(seed.topics),
                language=seed.language,
                proposed_url_scope=list(patterns),
                expected_hosts=list(seed.expected_hosts),
                facts={"verification_skipped": True},
                warnings=["live verification skipped"],
                recommendation="further_review",
                recommendation_rationale="verification was skipped",
            )
            manifest.sources.append(record)
        return manifest

    for seed, authority_name, patterns in sources:
        manifest.sources.append(
            deepen_source_verification(
                seed,
                authority_name=authority_name,
                path_prefix_patterns=patterns,
                http_client=http_client,
            )
        )
    return manifest


def host_of_url(url: str) -> str | None:
    return urlsplit(url).hostname
