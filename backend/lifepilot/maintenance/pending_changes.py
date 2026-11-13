"""Pending material/uncertain legal-change registry for agent awareness."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PENDING_PATH = ROOT / "data" / "runs" / "pending_material_changes.json"


def load_pending_changes(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_PENDING_PATH
    if not p.exists():
        return {
            "registry_id": "pending_material_changes_v1",
            "updated_at": None,
            "items": [],
        }
    return json.loads(p.read_text(encoding="utf-8"))


def save_pending_changes(data: dict[str, Any], path: Path | None = None) -> Path:
    p = path or DEFAULT_PENDING_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def upsert_pending_item(
    item: dict[str, Any],
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    data = load_pending_changes(path)
    items = list(data.get("items") or [])
    key = item.get("id")
    replaced = False
    for i, existing in enumerate(items):
        if existing.get("id") == key:
            items[i] = {**existing, **item}
            replaced = True
            break
    if not replaced:
        items.append(item)
    data["items"] = items
    save_pending_changes(data, path)
    return data


def pending_uncertainty_reasons(
    *,
    jurisdiction: str,
    pathway_id: str | None,
    path: Path | None = None,
) -> list[str]:
    jur = (jurisdiction or "").strip().upper()
    pid = (pathway_id or "").strip() or None
    reasons: list[str] = []
    for item in load_pending_changes(path).get("items") or []:
        if (item.get("jurisdiction") or "").upper() != jur:
            continue
        item_pid = (item.get("pathway_id") or "").strip() or None
        if item_pid and pid and item_pid != pid:
            continue
        if item_pid and not pid:
            continue
        status = (item.get("status") or "").lower()
        if status in {"pending_review", "potentially_material", "uncertain"}:
            reasons.append(
                f"pending_material_change:{item.get('id')}:{status}"
            )
    return reasons
