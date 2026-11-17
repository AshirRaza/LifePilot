"""Database and operational readiness probes."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from lifepilot.operations.secrets_redact import redact_secrets

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MAINTENANCE_DIR = REPO_ROOT / "data" / "runs" / "knowledge_maintenance"
DEFAULT_MAINTENANCE_REPORT = DEFAULT_MAINTENANCE_DIR / "latest_report.json"
DEFAULT_MAINTENANCE_REPORT_FALLBACK = (
    REPO_ROOT / "data" / "runs" / "knowledge_maintenance_report.json"
)
DEFAULT_FRESHNESS_ALERTS = DEFAULT_MAINTENANCE_DIR / "freshness_alerts.json"


def check_database() -> dict[str, Any]:
    """Return DB connectivity status without leaking connection strings."""
    started = datetime.now(timezone.utc)
    try:
        from lifepilot.db.session import get_engine

        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {
            "ok": True,
            "checked_at": started.isoformat(),
            "latency_ms": round(
                (datetime.now(timezone.utc) - started).total_seconds() * 1000, 2
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "checked_at": started.isoformat(),
            "error": redact_secrets(f"{exc.__class__.__name__}: {exc}"),
        }


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def readiness_payload(
    *,
    maintenance_report_path: Path | None = None,
    require_llm: bool | None = None,
) -> dict[str, Any]:
    """Assemble readiness details for operators (not a public status page)."""
    db = check_database()
    llm_provider = (os.environ.get("LIFEPILOT_LLM_PROVIDER") or "").strip()
    llm_model = (os.environ.get("LIFEPILOT_LLM_MODEL") or "").strip()
    has_anthropic = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    llm_ok = bool(llm_provider and llm_model and (has_anthropic or llm_provider != "anthropic"))
    if require_llm is None:
        require_llm = (os.environ.get("LIFEPILOT_API_REQUIRE_LLM") or "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    report_path = maintenance_report_path or DEFAULT_MAINTENANCE_REPORT
    report = _load_json(report_path)
    if report is None and maintenance_report_path is None:
        report = _load_json(DEFAULT_MAINTENANCE_REPORT_FALLBACK)
        if report is not None:
            report_path = DEFAULT_MAINTENANCE_REPORT_FALLBACK
    alerts = _load_json(DEFAULT_FRESHNESS_ALERTS) or {}
    overdue = list(alerts.get("overdue_sources") or [])
    pending = list(alerts.get("pending_material_changes") or [])

    ready = bool(db.get("ok")) and (llm_ok if require_llm else True)
    reasons: list[str] = []
    if not db.get("ok"):
        reasons.append("database_unavailable")
    if require_llm and not llm_ok:
        reasons.append("llm_not_configured")
    if overdue:
        reasons.append(f"overdue_sources:{len(overdue)}")
    if pending:
        reasons.append(f"pending_material_changes:{len(pending)}")

    return {
        "ready": ready,
        "database": db,
        "llm": {
            "configured": llm_ok,
            "provider": llm_provider or None,
            "model": llm_model or None,
            "required": require_llm,
        },
        "maintenance": {
            "report_present": report is not None,
            "report_path": str(report_path),
            "generated_at": (report or {}).get("generated_at"),
            "dry_run": (report or {}).get("dry_run"),
            "counts": (report or {}).get("counts"),
        },
        "freshness": {
            "overdue_count": len(overdue),
            "pending_material_count": len(pending),
            "alerts_path": str(DEFAULT_FRESHNESS_ALERTS.relative_to(REPO_ROOT)),
        },
        "reasons": reasons,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
