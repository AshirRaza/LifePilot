"""Attach current document_version ids to assessments from the live DB."""

from __future__ import annotations

from sqlalchemy import text

from lifepilot.coverage.evidence_remediation.assessments import FieldAssessment
from lifepilot.db.session import get_session_factory


def enrich_version_ids(assessments: list[FieldAssessment]) -> list[FieldAssessment]:
    doc_ids = sorted({a.document_id for a in assessments if a.document_id})
    if not doc_ids:
        return assessments
    session = get_session_factory()()
    try:
        rows = session.execute(
            text(
                """
                SELECT document_id, id AS version_id
                FROM document_versions
                WHERE is_current AND document_id = ANY(:ids)
                """
            ),
            {"ids": doc_ids},
        ).mappings().all()
        mapping = {int(r["document_id"]): int(r["version_id"]) for r in rows}
    finally:
        session.close()

    for a in assessments:
        if a.document_id and a.document_id in mapping:
            a.version_id = mapping[a.document_id]
    return assessments
