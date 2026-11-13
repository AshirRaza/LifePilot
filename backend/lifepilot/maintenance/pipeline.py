"""Knowledge-base maintenance: freshness → fetch → compare → classify → act."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.crawl.fetcher import (
    STATUS_FAILED,
    STATUS_FETCHED_NEW_VERSION,
    STATUS_UNCHANGED,
    fetch_html_document_url,
)
from lifepilot.crawl.storage import data_root, safe_read_bytes
from lifepilot.db.current_version import get_current_version, set_current_version
from lifepilot.db.models import Document, DocumentVersion, Source
from lifepilot.external.textutil import html_to_text
from lifepilot.maintenance.change_classify import ChangeClass, classify_content_change
from lifepilot.maintenance.pending_changes import upsert_pending_item
from lifepilot.maintenance.review_packets import (
    sync_high_risk_queue_from_pending,
    write_review_packet,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_POLICY = ROOT / "config" / "freshness" / "source_freshness_policy.json"
DEFAULT_REPORT = ROOT / "data" / "runs" / "knowledge_maintenance_report.json"


@dataclass
class MaintenanceItemResult:
    jurisdiction: str | None = None
    pathway_id: str | None = None
    document_id: int | None = None
    source_id: int | None = None
    canonical_url: str | None = None
    status: str = "skipped"
    change_class: str | None = None
    version_id: int | None = None
    previous_version_id: int | None = None
    message: str = ""
    reindexed: bool = False


@dataclass
class MaintenanceReport:
    generated_at: str = ""
    dry_run: bool = True
    limit: int | None = None
    jurisdiction: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _hours_since(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600.0


def _freshness_anchor(ver: DocumentVersion | None) -> datetime | None:
    if ver is None:
        return None
    return ver.last_checked_at or ver.retrieved_at


def _load_policy(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def find_due_documents(
    session: Session,
    policy: dict[str, Any],
    *,
    jurisdiction: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Identify priority documents due for recheck (stale or missing age)."""
    categories = policy.get("categories") or {}
    jur_filter = (jurisdiction or "").strip().upper() or None
    due: list[dict[str, Any]] = []
    docs = list(
        session.scalars(
            select(Document).where(Document.canonical_url.is_not(None)).limit(8000)
        )
    )
    for item in policy.get("priority_sources") or []:
        if jur_filter and (item.get("jurisdiction") or "").upper() != jur_filter:
            continue
        needle = (item.get("url_contains") or "").lower()
        cat = item.get("category") or "general_explanatory_guidance"
        limit_h = float((categories.get(cat) or {}).get("recheck_hours") or 336)
        matches = [
            d for d in docs if needle and needle in (d.canonical_url or "").lower()
        ]
        for doc in matches[:3]:
            ver = get_current_version(session, doc.id)
            age_h = _hours_since(_freshness_anchor(ver))
            if age_h is not None and age_h <= limit_h:
                continue
            due.append(
                {
                    "policy_item": item,
                    "document": doc,
                    "version": ver,
                    "recheck_hours": limit_h,
                    "age_hours": age_h,
                }
            )
            if limit is not None and len(due) >= limit:
                return due
    return due


def _version_text(storage_root: Path, ver: DocumentVersion | None) -> str:
    if ver is None or not ver.storage_path:
        return ""
    try:
        raw = safe_read_bytes(storage_root, ver.storage_path)
    except Exception:  # noqa: BLE001
        return ""
    if not raw:
        return ""
    ctype = (ver.content_type or "").lower()
    if "html" in ctype or ver.storage_path.endswith(".html"):
        return html_to_text(raw.decode("utf-8", errors="replace"))
    return raw.decode("utf-8", errors="replace")


def _mark_last_checked(session: Session, version: DocumentVersion) -> None:
    version.last_checked_at = datetime.now(timezone.utc)
    session.add(version)
    session.flush()


def _demote_new_keep_previous(
    session: Session,
    *,
    document_id: int,
    new_version: DocumentVersion,
    previous_version: DocumentVersion | None,
) -> None:
    """Keep historical new bytes but do not promote material changes yet."""
    new_version.is_current = False
    session.add(new_version)
    if previous_version is not None:
        set_current_version(session, previous_version)
    session.flush()


def run_knowledge_maintenance(
    session: Session,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    jurisdiction: str | None = None,
    policy_path: Path | None = None,
    process_editorial: bool = True,
    reindex_fn: Callable[[Session, int], bool] | None = None,
    sync_review_queue: bool = True,
) -> MaintenanceReport:
    """Recheck due approved sources; classify; process safe updates; queue material."""
    policy = _load_policy(policy_path or DEFAULT_POLICY)
    due = find_due_documents(
        session, policy, jurisdiction=jurisdiction, limit=limit
    )
    storage = data_root()
    results: list[MaintenanceItemResult] = []
    pending_for_sync: list[dict[str, Any]] = []

    for entry in due:
        doc: Document = entry["document"]
        item = entry["policy_item"]
        prev_ver: DocumentVersion | None = entry["version"]
        url = doc.canonical_url or ""
        source_id = doc.primary_source_id
        row = MaintenanceItemResult(
            jurisdiction=item.get("jurisdiction"),
            pathway_id=item.get("pathway_id"),
            document_id=doc.id,
            source_id=source_id,
            canonical_url=url,
        )
        if source_id is None:
            row.status = "skipped"
            row.message = "document has no primary_source_id"
            results.append(row)
            continue

        source = session.get(Source, source_id)
        if source is None or source.review_status != "approved":
            row.status = "skipped"
            row.message = "source missing or not approved"
            results.append(row)
            continue

        if dry_run:
            row.status = "dry_run"
            row.change_class = ChangeClass.UNCERTAIN.value
            row.message = (
                f"would recheck {url} (age_hours={entry.get('age_hours')})"
            )
            results.append(row)
            continue

        prev_text = _version_text(storage, prev_ver)
        fetch = fetch_html_document_url(
            session,
            source,
            url=url,
            discovery_status="freshness_recheck",
            persist=True,
        )
        if fetch.status in {STATUS_FAILED, "blocked_by_bot_protection", "invalid_content"}:
            row.status = "fetch_failed"
            row.change_class = ChangeClass.FETCH_FAILED.value
            row.message = fetch.message
            results.append(row)
            continue

        if fetch.status == STATUS_UNCHANGED:
            if prev_ver is not None:
                _mark_last_checked(session, prev_ver)
            row.status = "unchanged"
            row.change_class = ChangeClass.UNCHANGED.value
            row.version_id = prev_ver.id if prev_ver else None
            row.message = "content hash unchanged; last_checked_at updated"
            results.append(row)
            continue

        if fetch.status != STATUS_FETCHED_NEW_VERSION or fetch.version_id is None:
            row.status = "skipped"
            row.message = f"unexpected fetch status={fetch.status}"
            results.append(row)
            continue

        new_ver = session.get(DocumentVersion, fetch.version_id)
        new_text = _version_text(storage, new_ver)
        change = classify_content_change(
            previous_text=prev_text,
            new_text=new_text,
            fetch_ok=True,
            hashes_equal=False,
        )
        row.change_class = change.value
        row.version_id = fetch.version_id
        row.previous_version_id = prev_ver.id if prev_ver else None

        if change == ChangeClass.UNCHANGED:
            # Raw bytes can differ (cookies/CSRF) while normalized text matches.
            # Keep historical bytes but do not promote a redundant current version.
            if new_ver is not None and prev_ver is not None:
                _demote_new_keep_previous(
                    session,
                    document_id=doc.id,
                    new_version=new_ver,
                    previous_version=prev_ver,
                )
            if prev_ver is not None:
                _mark_last_checked(session, prev_ver)
            row.status = "unchanged"
            row.message = (
                "normalized text unchanged after recheck; "
                "last_checked_at updated; new bytes not promoted"
            )
            results.append(row)
            continue

        if change in {ChangeClass.POTENTIALLY_MATERIAL, ChangeClass.UNCERTAIN}:
            if new_ver is not None:
                _demote_new_keep_previous(
                    session,
                    document_id=doc.id,
                    new_version=new_ver,
                    previous_version=prev_ver,
                )
            pending = {
                "id": f"chg_{doc.id}_{fetch.version_id}",
                "jurisdiction": item.get("jurisdiction"),
                "pathway_id": item.get("pathway_id"),
                "document_id": doc.id,
                "previous_version_id": prev_ver.id if prev_ver else None,
                "new_version_id": fetch.version_id,
                "change_class": change.value,
                "status": "pending_review",
                "canonical_url": url,
                "claim": (
                    f"Possible material update for {item.get('pathway_id') or url}"
                ),
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            upsert_pending_item(pending)
            pending_for_sync.append(pending)
            write_review_packet(
                {
                    "id": pending["id"],
                    "jurisdiction": pending["jurisdiction"],
                    "pathway_id": pending["pathway_id"],
                    "claim": pending["claim"],
                    "change_class": change.value,
                    "status": "pending_review",
                    "recommended_action": (
                        "Compare old/new official text; approve new version "
                        "only after human legal review."
                    ),
                    "previous_url": url,
                    "new_url": fetch.final_url or url,
                    "checked_at": pending["checked_at"],
                    "effective_date": None,
                    "notes": (
                        "New version stored but not promoted to is_current. "
                        "Agent should treat affected claims as unverified."
                    ),
                }
            )
            row.status = "pending_review"
            row.message = "material/uncertain change queued; previous version kept current"
            results.append(row)
            continue

        # Editorial: keep new current; optional reindex.
        if new_ver is not None:
            _mark_last_checked(session, new_ver)
        row.status = "editorial_applied"
        row.message = "new version promoted as editorial/safe"
        if process_editorial and reindex_fn is not None and new_ver is not None:
            try:
                row.reindexed = bool(reindex_fn(session, doc.id))
            except Exception as exc:  # noqa: BLE001
                logger.exception("maintenance.reindex_failed doc=%s", doc.id)
                row.message += f"; reindex_failed:{exc.__class__.__name__}"
        results.append(row)

    if sync_review_queue and pending_for_sync and not dry_run:
        sync_high_risk_queue_from_pending(pending_for_sync)

    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    report = MaintenanceReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        dry_run=dry_run,
        limit=limit,
        jurisdiction=jurisdiction,
        counts=counts,
        items=[asdict(r) for r in results],
        limitations=[
            "Material/uncertain changes are stored but not promoted to is_current.",
            "Editorial auto-promotion is conservative text classification, not legal verification.",
            "last_checked_at updates do not rewrite retrieved_at provenance.",
            "Failed fetches are never treated as unchanged.",
            "This command is not a continuous scheduler; invoke via Task Scheduler/cron/CI.",
        ],
    )
    return report


def default_reindex(session: Session, document_id: int) -> bool:
    """Best-effort extract → chunk for one document (embeddings optional)."""
    from lifepilot.extract.runner import extract_document_version
    from lifepilot.rag.pipeline import run_chunking

    ver = get_current_version(session, document_id)
    if ver is None:
        return False
    doc = session.get(Document, document_id)
    if doc is None:
        return False
    extract_document_version(session, ver, doc, persist=True)
    run_chunking(session, document_ids=[document_id], force=True)
    return True
