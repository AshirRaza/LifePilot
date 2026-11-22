"""Add persistent legal review governance tables (Phase 10D.1).

Revision ID: 20261122_0012
Revises: 20261118_0011
Create Date: 2026-11-22

Additive, reversible schema for:
- legal_review_items (fail-closed pending review work items)
- legal_review_events (append-only audit history with UPDATE/DELETE triggers)

Does not mutate existing LegalRule / DocumentVersion rows or promote any
verification_status to human_verified.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261122_0012"
down_revision: Union[str, Sequence[str], None] = "20261118_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SUBJECT_TYPES = "'legal_rule', 'claim_evidence', 'high_risk_queue_item'"
_WORKFLOW_STATES = (
    "'pending', 'assigned', 'in_review', 'approved', 'rejected', "
    "'changes_requested', 'reopened', 'invalidated', 'superseded'"
)
_EVENT_TYPES = (
    "'submitted_for_review', 'assigned', 'review_started', 'approved', "
    "'rejected', 'changes_requested', 'reopened', 'approval_invalidated', "
    "'source_version_changed', 'superseded', 'administrative_correction'"
)
_ACTOR_KINDS = "'human_reviewer', 'service', 'system'"
_RISK_CLASSES = "'critical', 'high', 'medium', 'low'"
_PRIORITIES = "'p0', 'p1', 'p2', 'p3'"


def upgrade() -> None:
    op.create_table(
        "legal_review_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "public_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("legal_rule_id", sa.BigInteger(), sa.ForeignKey("legal_rules.id"), nullable=True),
        sa.Column("subject_ref", sa.Text(), nullable=True),
        sa.Column(
            "jurisdiction_id",
            sa.BigInteger(),
            sa.ForeignKey("jurisdictions.id"),
            nullable=False,
        ),
        sa.Column("pathway_id", sa.Text(), nullable=True),
        sa.Column("requirement_category", sa.Text(), nullable=True),
        sa.Column("risk_class", sa.Text(), nullable=False, server_default=sa.text("'high'")),
        sa.Column(
            "workflow_state", sa.Text(), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("review_reason", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=False, server_default=sa.text("'p1'")),
        sa.Column("source_id", sa.BigInteger(), sa.ForeignKey("sources.id"), nullable=True),
        sa.Column("document_id", sa.BigInteger(), sa.ForeignKey("documents.id"), nullable=True),
        sa.Column(
            "document_version_id",
            sa.BigInteger(),
            sa.ForeignKey("document_versions.id"),
            nullable=True,
        ),
        sa.Column("evidence_fingerprint", sa.Text(), nullable=True),
        sa.Column("reviewed_effective_from", sa.Date(), nullable=True),
        sa.Column("reviewed_effective_to", sa.Date(), nullable=True),
        sa.Column("reviewed_cohort_key", sa.Text(), nullable=True),
        sa.Column("assigned_actor_id", sa.Text(), nullable=True),
        sa.Column("assigned_actor_kind", sa.Text(), nullable=True),
        sa.Column("review_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reassessment_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("invalidation_reason", sa.Text(), nullable=True),
        sa.Column("current_approved_event_id", sa.BigInteger(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("public_id", name="uq_legal_review_items_public_id"),
        sa.CheckConstraint(
            f"subject_type IN ({_SUBJECT_TYPES})",
            name="ck_legal_review_items_subject_type",
        ),
        sa.CheckConstraint(
            f"workflow_state IN ({_WORKFLOW_STATES})",
            name="ck_legal_review_items_workflow_state",
        ),
        sa.CheckConstraint(
            f"risk_class IN ({_RISK_CLASSES})",
            name="ck_legal_review_items_risk_class",
        ),
        sa.CheckConstraint(
            f"priority IN ({_PRIORITIES})",
            name="ck_legal_review_items_priority",
        ),
        sa.CheckConstraint(
            "length(trim(review_reason)) > 0",
            name="ck_legal_review_items_review_reason_nonempty",
        ),
        sa.CheckConstraint(
            "document_version_id IS NOT NULL OR ("
            "evidence_fingerprint IS NOT NULL AND length(trim(evidence_fingerprint)) > 0"
            ")",
            name="ck_legal_review_items_requires_evidence_pin",
        ),
        sa.CheckConstraint(
            "subject_type <> 'legal_rule' OR ("
            "legal_rule_id IS NOT NULL AND document_version_id IS NOT NULL"
            ")",
            name="ck_legal_review_items_legal_rule_requires_rule_and_version",
        ),
        sa.CheckConstraint(
            f"assigned_actor_kind IS NULL OR assigned_actor_kind IN ({_ACTOR_KINDS})",
            name="ck_legal_review_items_assigned_actor_kind",
        ),
        sa.CheckConstraint(
            "(assigned_actor_id IS NULL AND assigned_actor_kind IS NULL) OR ("
            "assigned_actor_id IS NOT NULL AND length(trim(assigned_actor_id)) > 0 "
            "AND assigned_actor_kind IS NOT NULL"
            ")",
            name="ck_legal_review_items_assigned_actor_pair",
        ),
        sa.CheckConstraint(
            "row_version >= 1",
            name="ck_legal_review_items_row_version_positive",
        ),
    )
    op.create_index(
        "ix_legal_review_items_workflow_state", "legal_review_items", ["workflow_state"]
    )
    op.create_index(
        "ix_legal_review_items_jurisdiction_id", "legal_review_items", ["jurisdiction_id"]
    )
    op.create_index("ix_legal_review_items_pathway_id", "legal_review_items", ["pathway_id"])
    op.create_index(
        "ix_legal_review_items_legal_rule_id", "legal_review_items", ["legal_rule_id"]
    )
    op.create_index(
        "ix_legal_review_items_document_version_id",
        "legal_review_items",
        ["document_version_id"],
    )
    op.create_index(
        "ix_legal_review_items_priority_state",
        "legal_review_items",
        ["priority", "workflow_state"],
    )
    op.create_index(
        "ix_legal_review_items_assigned_actor_id",
        "legal_review_items",
        ["assigned_actor_id"],
    )

    op.create_table(
        "legal_review_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "review_item_id",
            sa.BigInteger(),
            sa.ForeignKey("legal_review_items.id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("from_workflow_state", sa.Text(), nullable=True),
        sa.Column("to_workflow_state", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("evidence_fingerprint", sa.Text(), nullable=True),
        sa.Column(
            "document_version_id",
            sa.BigInteger(),
            sa.ForeignKey("document_versions.id"),
            nullable=True,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint(
            f"event_type IN ({_EVENT_TYPES})",
            name="ck_legal_review_events_event_type",
        ),
        sa.CheckConstraint(
            f"actor_kind IN ({_ACTOR_KINDS})",
            name="ck_legal_review_events_actor_kind",
        ),
        sa.CheckConstraint(
            "length(trim(actor_id)) > 0",
            name="ck_legal_review_events_actor_id_nonempty",
        ),
        sa.CheckConstraint(
            f"from_workflow_state IS NULL OR from_workflow_state IN ({_WORKFLOW_STATES})",
            name="ck_legal_review_events_from_state",
        ),
        sa.CheckConstraint(
            f"to_workflow_state IN ({_WORKFLOW_STATES})",
            name="ck_legal_review_events_to_state",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metadata) = 'object'",
            name="ck_legal_review_events_metadata_object",
        ),
    )
    op.create_index(
        "ix_legal_review_events_review_item_id",
        "legal_review_events",
        ["review_item_id"],
    )
    op.create_index(
        "ix_legal_review_events_event_type", "legal_review_events", ["event_type"]
    )
    op.create_index(
        "ix_legal_review_events_occurred_at", "legal_review_events", ["occurred_at"]
    )
    op.create_index(
        "ix_legal_review_events_actor_id", "legal_review_events", ["actor_id"]
    )

    # Optional pointer from item → approving event (added after events table exists).
    op.create_foreign_key(
        "fk_legal_review_items_current_approved_event_id",
        "legal_review_items",
        "legal_review_events",
        ["current_approved_event_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Append-only protections for ordinary application roles.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_legal_review_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'legal_review_events is append-only; UPDATE/DELETE is forbidden '
                '(administrative recovery requires documented DBA procedure)';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_legal_review_events_no_update
        BEFORE UPDATE ON legal_review_events
        FOR EACH ROW
        EXECUTE FUNCTION prevent_legal_review_event_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_legal_review_events_no_delete
        BEFORE DELETE ON legal_review_events
        FOR EACH ROW
        EXECUTE FUNCTION prevent_legal_review_event_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_legal_review_events_no_delete ON legal_review_events")
    op.execute("DROP TRIGGER IF EXISTS trg_legal_review_events_no_update ON legal_review_events")
    op.execute("DROP FUNCTION IF EXISTS prevent_legal_review_event_mutation()")
    op.drop_constraint(
        "fk_legal_review_items_current_approved_event_id",
        "legal_review_items",
        type_="foreignkey",
    )
    op.drop_table("legal_review_events")
    op.drop_table("legal_review_items")
