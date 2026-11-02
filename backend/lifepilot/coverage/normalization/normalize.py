"""Normalize Phase 6E pathway inventory into pathway / hub / unknown entities."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

EntityType = Literal["pathway", "information_hub", "unknown_slot"]


@dataclass
class NormalizedEntity:
    original_pathway_id: str
    normalized_id: str
    entity_type: EntityType
    country: str
    name: str
    parent_category: str
    program_identity: str | None
    hub_role: str | None
    original_status: str
    source_ids: list[int] = field(default_factory=list)
    document_ids: list[int] = field(default_factory=list)
    financial_applicable: bool = True
    evidence_thin: bool = True
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _config_path() -> Path:
    return Path(__file__).resolve().parents[4] / "config" / "coverage" / "pathway_normalization_6e1.json"


@lru_cache(maxsize=1)
def load_normalization_config(path: str | None = None) -> dict[str, Any]:
    cfg = Path(path) if path else _config_path()
    return json.loads(cfg.read_text(encoding="utf-8"))


def build_entity_index(
    config: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    cfg = config or load_normalization_config()
    return {e["original_pathway_id"]: e for e in cfg.get("entities", [])}


def classify_unknown_id(original_pathway_id: str) -> bool:
    return "_unknown_" in original_pathway_id or original_pathway_id.endswith(
        tuple(
            f"_unknown_{slot}"
            for slot in (
                "work",
                "study",
                "family",
                "permanent_residence",
                "citizenship",
                "eu_residence",
            )
        )
    ) or "unknown_" in original_pathway_id


def normalize_inventory(
    inventory: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
) -> tuple[list[NormalizedEntity], list[dict[str, str]]]:
    """Map original 6E pathways to normalized entities. Preserve all original IDs."""
    cfg = config or load_normalization_config()
    index = build_entity_index(cfg)
    entities: list[NormalizedEntity] = []
    mapping: list[dict[str, str]] = []

    for raw in inventory.get("pathways", []):
        oid = str(raw["pathway_id"])
        country = str(raw.get("country") or "").upper()
        name = str(raw.get("name") or oid)
        status = str(raw.get("status") or "")
        parent = str(raw.get("parent_category") or "")

        if oid in index:
            spec = index[oid]
            etype: EntityType = spec["entity_type"]  # type: ignore[assignment]
            nid = str(spec["normalized_id"])
            program = spec.get("program_identity")
            hub_role = spec.get("hub_role")
        elif status == "unknown_or_not_inventoried" or classify_unknown_id(oid):
            etype = "unknown_slot"
            nid = oid
            program = None
            hub_role = None
        else:
            # Unmapped inventoried row: treat as hub until program identity proven
            etype = "information_hub"
            nid = f"{oid}__hub"
            program = None
            hub_role = "unmapped_6e_entity_defaulted_to_hub"
            notes_extra = ["6E.1: unmapped inventoried ID defaulted to information_hub"]
            raw_notes = list(raw.get("notes") or []) + notes_extra
            entities.append(
                NormalizedEntity(
                    original_pathway_id=oid,
                    normalized_id=nid,
                    entity_type=etype,
                    country=country,
                    name=name,
                    parent_category=parent,
                    program_identity=program,
                    hub_role=hub_role,
                    original_status=status,
                    source_ids=list(raw.get("source_ids") or []),
                    document_ids=list(raw.get("document_ids") or []),
                    financial_applicable=bool(raw.get("financial_applicable", True)),
                    evidence_thin=bool(raw.get("evidence_thin", True)),
                    notes=raw_notes,
                )
            )
            mapping.append(
                {
                    "original_pathway_id": oid,
                    "normalized_id": nid,
                    "entity_type": etype,
                }
            )
            continue

        entities.append(
            NormalizedEntity(
                original_pathway_id=oid,
                normalized_id=nid,
                entity_type=etype,
                country=country,
                name=name,
                parent_category=parent,
                program_identity=program if isinstance(program, str) else None,
                hub_role=hub_role if isinstance(hub_role, str) else None,
                original_status=status,
                source_ids=list(raw.get("source_ids") or []),
                document_ids=list(raw.get("document_ids") or []),
                financial_applicable=bool(raw.get("financial_applicable", True)),
                evidence_thin=bool(raw.get("evidence_thin", True)),
                notes=list(raw.get("notes") or []),
            )
        )
        mapping.append(
            {
                "original_pathway_id": oid,
                "normalized_id": nid,
                "entity_type": etype,
            }
        )

    return entities, mapping


def true_pathways(entities: list[NormalizedEntity]) -> list[NormalizedEntity]:
    return [e for e in entities if e.entity_type == "pathway"]


def information_hubs(entities: list[NormalizedEntity]) -> list[NormalizedEntity]:
    return [e for e in entities if e.entity_type == "information_hub"]


def unknown_slots(entities: list[NormalizedEntity]) -> list[NormalizedEntity]:
    return [e for e in entities if e.entity_type == "unknown_slot"]
