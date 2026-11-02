"""Validate owner-authorized URLs from the probe JSON (or a live recheck) and decide."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lifepilot.coverage.source_expansion.candidates import (
    PLAN_EXISTING_SCOPE,
    Candidate,
    safe_normalize,
)
from lifepilot.registry.urls import url_in_any_allowed_pattern

NOT_FOUND_STATUSES = {404, 410}


def load_probe(path: Path) -> dict[str, dict[str, Any]]:
    """Load ``_probe_6h_url_validation.json`` keyed by authorized URL."""
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    return {str(r["url"]): r for r in rows}


def recheck_robots(url: str) -> dict[str, Any]:
    """Re-check only ``/robots.txt`` (never the page) for rows with unknown robots."""
    from lifepilot.crawl.policy import fetcher_policy
    from lifepilot.crawl.robots import check_robots

    policy = fetcher_policy()
    decision = check_robots(url, policy)
    return {
        "fetched": decision.fetched,
        "status_code": decision.status_code,
        "can_fetch": decision.can_fetch,
        "error": decision.error,
        "summary": decision.summary,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def recheck_candidate(candidate: Candidate) -> dict[str, Any]:
    """Live validation (robots + GET) equivalent to the probe script."""
    import httpx

    from lifepilot.crawl.policy import fetcher_policy
    from lifepilot.crawl.robots import check_robots

    policy = fetcher_policy()
    url = candidate.authorized_url
    row: dict[str, Any] = {
        "jurisdiction": candidate.jurisdiction,
        "url": url,
        "expected_hosts": list(candidate.expected_hosts),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "robots": None,
        "http_status": None,
        "final_url": None,
        "final_host": None,
        "content_type": None,
        "redirect_chain_hosts": [],
        "host_ok": False,
        "accessible": False,
        "error": None,
        "snippet": "",
        "relevance_keywords_hit": False,
    }
    with httpx.Client(
        follow_redirects=True,
        timeout=30.0,
        headers={"User-Agent": policy.user_agent},
        verify=True,
    ) as client:
        try:
            robots = check_robots(url, policy, client=client)
            row["robots"] = {
                "fetched": robots.fetched,
                "status_code": robots.status_code,
                "can_fetch": robots.can_fetch,
                "error": robots.error,
                "summary": robots.summary,
            }
            if robots.can_fetch is False:
                row["error"] = "robots_disallowed"
                return row
            resp = client.get(url)
            row["http_status"] = resp.status_code
            row["final_url"] = str(resp.url)
            row["final_host"] = urlsplit(str(resp.url)).hostname
            row["content_type"] = resp.headers.get("content-type")
            row["redirect_chain_hosts"] = [
                urlsplit(str(h.url)).hostname for h in resp.history
            ] + [row["final_host"]]
            row["host_ok"] = row["final_host"] in set(candidate.expected_hosts)
            row["accessible"] = 200 <= resp.status_code < 400
            ctype = (row["content_type"] or "").lower()
            text = ""
            if "pdf" in ctype:
                text = f"[pdf bytes={len(resp.content)}]"
            elif "html" in ctype:
                text = resp.text[:1500]
            row["snippet"] = text.replace("\n", " ")[:500]
            row["relevance_keywords_hit"] = _keyword_hit(text, url)
        except Exception as exc:  # noqa: BLE001
            row["error"] = str(exc)
    return row


def _keyword_hit(text: str, url: str) -> bool:
    lower = f"{text} {url}".lower()
    return any(
        k in lower
        for k in (
            "student",
            "visa",
            "permit",
            "checklist",
            "blue card",
            "blaue karte",
            "passeport talent",
            "engineer",
            "citizenship",
            "employment",
            "graduate",
            "residence",
            "emirates",
        )
    )


def _title_from_snippet(snippet: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", snippet or "", flags=re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


@dataclass
class Decision:
    candidate_id: str
    jurisdiction: str
    pathway_ids: tuple[str, ...]
    authorized_url: str
    final_url: str
    plan: str
    decision: str  # approved | rejected | pending_validation
    decision_code: str
    reason: str
    restrictions: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    source_id: int | None = None
    registration_url: str = ""
    decided_at: str = ""

    def to_row(self) -> dict[str, Any]:
        chk = self.checks
        return {
            "candidate_id": self.candidate_id,
            "jurisdiction": self.jurisdiction,
            "pathway_ids": ",".join(self.pathway_ids),
            "authorized_url": self.authorized_url,
            "final_url": self.final_url,
            "registration_url": self.registration_url,
            "plan": self.plan,
            "decision": self.decision,
            "decision_code": self.decision_code,
            "reason": self.reason,
            "http_status": chk.get("http_status"),
            "content_type": chk.get("content_type"),
            "redirect_chain_hosts": ">".join(str(h) for h in chk.get("redirect_chain_hosts", [])),
            "final_host": chk.get("final_host"),
            "host_ok": chk.get("host_ok"),
            "robots_summary": chk.get("robots_summary"),
            "robots_can_fetch": chk.get("robots_can_fetch"),
            "relevance_keywords_hit": chk.get("relevance_keywords_hit"),
            "page_title": chk.get("page_title"),
            "restrictions": " | ".join(self.restrictions),
            "source_id": self.source_id or "",
            "validated_at": chk.get("checked_at"),
            "decided_at": self.decided_at,
        }


def decide(
    candidate: Candidate,
    probe: dict[str, Any] | None,
    *,
    source_patterns: list[str] | None = None,
    robots_recheck: dict[str, Any] | None = None,
    decided_at: str | None = None,
) -> Decision:
    """Apply the owner's validation checklist to one candidate (no DB writes)."""
    stamp = decided_at or datetime.now(timezone.utc).isoformat()
    base = dict(
        candidate_id=candidate.candidate_id,
        jurisdiction=candidate.jurisdiction,
        pathway_ids=candidate.pathway_ids,
        authorized_url=candidate.authorized_url,
        plan=candidate.plan,
        source_id=candidate.source_id,
        decided_at=stamp,
    )
    if probe is None:
        return Decision(
            **base,
            final_url="",
            decision="pending_validation",
            decision_code="pending_validation",
            reason="No validation evidence available; not approved.",
        )

    robots = probe.get("robots") or {}
    can_fetch = robots.get("can_fetch")
    robots_summary = robots.get("summary")
    recheck_note = ""
    if can_fetch is None and robots_recheck is not None:
        can_fetch = robots_recheck.get("can_fetch")
        robots_summary = f"{robots_summary} | recheck: {robots_recheck.get('summary')}"
        recheck_note = "robots re-check performed for unknown robots status"
    final_url = str(probe.get("final_url") or "")
    http_status = probe.get("http_status")
    title = _title_from_snippet(str(probe.get("snippet") or ""))
    checks = {
        "http_status": http_status,
        "content_type": probe.get("content_type"),
        "redirect_chain_hosts": probe.get("redirect_chain_hosts") or [],
        "final_host": probe.get("final_host"),
        "host_ok": bool(probe.get("host_ok")),
        "robots_summary": robots_summary,
        "robots_can_fetch": can_fetch,
        "relevance_keywords_hit": probe.get("relevance_keywords_hit"),
        "page_title": title,
        "checked_at": probe.get("checked_at"),
        "recheck_note": recheck_note,
    }

    def reject(code: str, reason: str) -> Decision:
        return Decision(
            **base,
            final_url=final_url,
            decision="rejected",
            decision_code=code,
            reason=reason,
            checks=checks,
            restrictions=["do_not_fetch", "no_silent_substitution"],
        )

    if can_fetch is False:
        return reject("rejected_robots_disallowed", "robots.txt disallows this URL.")
    chain = [h for h in (probe.get("redirect_chain_hosts") or []) if h]
    expected = set(candidate.expected_hosts)
    if not probe.get("host_ok") or any(h not in expected for h in chain):
        return reject(
            "rejected_redirect_host_mismatch",
            f"Final/redirect host not in expected official hosts {sorted(expected)}.",
        )
    if not probe.get("accessible"):
        label = (
            candidate.not_accessible_label
            if http_status in NOT_FOUND_STATUSES or http_status is None
            else "rejected_inaccessible"
        )
        return reject(
            label,
            f"HTTP {http_status}: authorized URL is not accessible; "
            "replacement (if any) requires separate approval.",
        )
    hub_hit = any(m in title.lower() for m in candidate.hub_title_markers)
    if (not probe.get("relevance_keywords_hit") and not candidate.relevance_waiver) or hub_hit:
        return reject(
            "rejected_insufficient_pathway_relevance",
            (
                f"Page title {title!r} indicates a generic hub or pathway keywords were "
                "not found; not a passeport-talent pathway source. "
                f"Final URL: {final_url}."
            ),
        )

    restrictions = [
        "exact_url_or_narrowest_scope_only",
        "no_wildcard_beyond_path_prefix",
        "no_whole_domain_approval",
    ]
    if can_fetch is None:
        restrictions.append(
            "robots_unconfirmed_at_validation: fetch only if robots permits at fetch time"
        )
    if candidate.relevance_waiver:
        restrictions.append(f"relevance_note: {candidate.relevance_waiver}")

    registration_url = final_url or candidate.authorized_url
    if candidate.plan == PLAN_EXISTING_SCOPE:
        patterns = list(source_patterns or [])
        if not url_in_any_allowed_pattern(registration_url, patterns):
            return Decision(
                **base,
                final_url=final_url,
                decision="rejected",
                decision_code="rejected_outside_existing_scope",
                reason="Existing source allowlist does not cover this URL; no pattern change authorized.",
                checks=checks,
                restrictions=["do_not_fetch"],
            )
        code = "approved_existing_scope"
        reason = (
            "Validation passed; URL already covered by existing source allowlist "
            "(no pattern change)."
        )
    elif candidate.plan == "exact_allowlist":
        code = "approved_exact_allowlist"
        reason = "Validation passed; exact URL added to existing source allowlist."
    else:
        code = "approved_new_source"
        reason = "Validation passed; new narrowly scoped source registered."

    if safe_normalize(registration_url) != safe_normalize(candidate.authorized_url):
        restrictions.append(
            f"registered_final_url_differs_from_authorized: {registration_url}"
        )
    return Decision(
        **base,
        final_url=final_url,
        registration_url=registration_url,
        decision="approved",
        decision_code=code,
        reason=reason,
        checks=checks,
        restrictions=restrictions,
    )
