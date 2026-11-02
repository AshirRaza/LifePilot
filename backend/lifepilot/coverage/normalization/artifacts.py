"""Load Phase 6E artifacts (read-only) into typed structures."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from lifepilot.coverage.schema import CoverageClass, EvidenceRef, RequirementCell
from lifepilot.coverage.taxonomy import is_critical_field


def default_runs_dir(repo_root: Path | None = None) -> Path:
    root = repo_root or Path(__file__).resolve().parents[4]
    return root / "data" / "runs"


def load_inventory(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_matrix_csv(path: Path) -> list[RequirementCell]:
    cells: list[RequirementCell] = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            applicable = str(row.get("applicable", "True")).lower() in {
                "1",
                "true",
                "yes",
            }
            financial_app = True
            # Infer critical using taxonomy; financial applicability from later entity
            field = row["requirement_field"]
            is_crit = is_critical_field(field, financial_applicable=True)
            if field == "financial_or_sponsorship" and not applicable:
                is_crit = False
            excerpt = (row.get("evidence_excerpt") or "").strip()
            refs: list[EvidenceRef] = []
            if excerpt:
                refs.append(
                    EvidenceRef(
                        document_id=0,
                        version_id=0,
                        extractor="phase6e_matrix",
                        heading_or_excerpt=excerpt,
                    )
                )
            notes = [
                n.strip()
                for n in (row.get("notes") or "").split("|")
                if n.strip()
            ]
            cells.append(
                RequirementCell(
                    pathway_id=row["pathway_id"],
                    country=row["country"],
                    requirement_field=field,
                    coverage_class=CoverageClass(row["coverage_class"]),
                    evidence_refs=refs,
                    needs_human_review=str(
                        row.get("needs_human_review", "False")
                    ).lower()
                    in {"1", "true", "yes"},
                    notes=notes,
                    is_critical=is_crit
                    if str(row.get("is_critical", "")).lower()
                    in {"", "true", "1", "yes"}
                    else str(row.get("is_critical", "")).lower()
                    in {"1", "true", "yes"},
                    applicable=applicable,
                )
            )
    # Fix is_critical from CSV when present
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    for cell, row in zip(cells, rows, strict=False):
        if "is_critical" in row and row["is_critical"] != "":
            cell.is_critical = str(row["is_critical"]).lower() in {
                "1",
                "true",
                "yes",
            }
    return cells


def load_gap_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def enrich_cells_with_corpus_refs(
    cells: list[RequirementCell],
    *,
    document_ids_by_pathway: dict[str, list[int]],
    version_by_document: dict[int, int],
    extractor_by_document: dict[int, str],
) -> list[RequirementCell]:
    """Attach real document/version ids onto matrix excerpts when possible."""
    out: list[RequirementCell] = []
    for cell in cells:
        docs = document_ids_by_pathway.get(cell.pathway_id) or []
        refs = list(cell.evidence_refs)
        if refs and docs:
            doc_id = docs[0]
            refs = [
                EvidenceRef(
                    document_id=doc_id,
                    version_id=version_by_document.get(doc_id, 0),
                    extractor=extractor_by_document.get(doc_id, "html_structured"),
                    heading_or_excerpt=refs[0].heading_or_excerpt,
                )
            ]
        out.append(
            RequirementCell(
                pathway_id=cell.pathway_id,
                country=cell.country,
                requirement_field=cell.requirement_field,
                coverage_class=cell.coverage_class,
                evidence_refs=refs,
                needs_human_review=cell.needs_human_review,
                notes=list(cell.notes),
                is_critical=cell.is_critical,
                applicable=cell.applicable,
            )
        )
    return out
