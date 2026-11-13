"""Generate actionable human-review packets for material/uncertain changes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PACKET_DIR = ROOT / "data" / "runs" / "review_packets"
HIGH_RISK_QUEUE = ROOT / "config" / "eval" / "high_risk_legal_review_queue.json"


def write_review_packet(packet: dict[str, Any], *, directory: Path | None = None) -> Path:
    out_dir = directory or DEFAULT_PACKET_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    packet_id = packet.get("id") or f"packet_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    path = out_dir / f"{packet_id}.json"
    payload = {
        **packet,
        "id": packet_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "human_verified": False,
        "verification_note": (
            "Do not mark human_verified unless an authorized reviewer signs off."
        ),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path = out_dir / f"{packet_id}.md"
    md_path.write_text(_packet_markdown(payload), encoding="utf-8")
    return path


def _packet_markdown(packet: dict[str, Any]) -> str:
    lines = [
        f"# Review packet: {packet.get('id')}",
        "",
        f"- Jurisdiction: {packet.get('jurisdiction')}",
        f"- Pathway: {packet.get('pathway_id')}",
        f"- Classification: {packet.get('change_class')}",
        f"- Status: {packet.get('status')}",
        f"- Recommended action: {packet.get('recommended_action')}",
        f"- Human verified: {packet.get('human_verified')}",
        "",
        "## Claim / rule",
        packet.get("claim") or "(none)",
        "",
        "## Source URLs",
        f"- Previous: {packet.get('previous_url')}",
        f"- New: {packet.get('new_url')}",
        "",
        "## Timestamps",
        f"- Retrieved / checked: {packet.get('checked_at')}",
        f"- Effective date (if known): {packet.get('effective_date')}",
        "",
        "## Notes",
        packet.get("notes") or "",
        "",
    ]
    return "\n".join(lines) + "\n"


def sync_high_risk_queue_from_pending(
    pending_items: list[dict[str, Any]],
    *,
    queue_path: Path | None = None,
) -> Path:
    """Update matching high-risk queue rows to potentially_outdated / conflicting.

    Never sets human_verified.
    """
    path = queue_path or HIGH_RISK_QUEUE
    data = json.loads(path.read_text(encoding="utf-8"))
    items = list(data.get("items") or [])
    by_key = {
        (i.get("jurisdiction"), i.get("pathway_id")): i for i in pending_items
    }
    for row in items:
        key = (row.get("jurisdiction"), row.get("pathway_id"))
        pending = by_key.get(key)
        if not pending:
            continue
        status = (row.get("verification_status") or "").lower()
        if status == "human_verified":
            continue
        row["verification_status"] = "potentially_outdated"
        row["uncertainty"] = (
            f"Pending material/uncertain change {pending.get('id')}; "
            f"class={pending.get('change_class')}"
        )
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
