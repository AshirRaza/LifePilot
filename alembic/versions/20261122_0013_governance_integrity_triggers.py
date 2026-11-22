"""Phase 10D.4 — DB enforcement for evented review updates and LegalRule promotion.

Revision ID: 20261122_0013
Revises: 20261122_0012
Create Date: 2026-11-22

Adds triggers that:
1. Reject legal_review_items workflow/invalidation/approval-pointer changes unless
   the latest legal_review_events row matches the new workflow_state (evented path).
2. Reject LegalRule → human_verified unless a linked approved, non-invalidated
   review item with matching document_version pin and human approval event exists.

Demotion of human_verified is allowed (fail closed). Direct SQL by the ordinary
app role cannot forge approvals without corresponding event rows.

DBA recovery: DISABLE TRIGGER USER on the affected tables, apply correction,
re-ENABLE, and insert an administrative_correction / audit note.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "20261122_0013"
down_revision: Union[str, Sequence[str], None] = "20261122_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_legal_review_item_evented_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            latest_to_state text;
            latest_event_id bigint;
        BEGIN
            IF TG_OP <> 'UPDATE' THEN
                RETURN NEW;
            END IF;

            -- Only protect governance state columns.
            IF (OLD.workflow_state IS NOT DISTINCT FROM NEW.workflow_state)
               AND (OLD.invalidated IS NOT DISTINCT FROM NEW.invalidated)
               AND (OLD.current_approved_event_id IS NOT DISTINCT FROM NEW.current_approved_event_id)
               AND (OLD.row_version IS NOT DISTINCT FROM NEW.row_version)
               AND (OLD.assigned_actor_id IS NOT DISTINCT FROM NEW.assigned_actor_id)
               AND (OLD.invalidation_reason IS NOT DISTINCT FROM NEW.invalidation_reason)
            THEN
                RETURN NEW;
            END IF;

            SELECT e.id, e.to_workflow_state
              INTO latest_event_id, latest_to_state
              FROM legal_review_events e
             WHERE e.review_item_id = NEW.id
             ORDER BY e.id DESC
             LIMIT 1;

            IF latest_event_id IS NULL THEN
                RAISE EXCEPTION
                    'eventless legal_review_items mutation forbidden (no events for item %)',
                    NEW.id;
            END IF;

            IF latest_to_state IS DISTINCT FROM NEW.workflow_state THEN
                RAISE EXCEPTION
                    'eventless legal_review_items mutation forbidden '
                    '(latest event to_state=% does not match workflow_state=%)',
                    latest_to_state, NEW.workflow_state;
            END IF;

            -- row_version must advance when governance fields change.
            IF NEW.row_version < OLD.row_version THEN
                RAISE EXCEPTION 'legal_review_items.row_version cannot decrease';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_legal_review_items_evented_update
            ON legal_review_items;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_legal_review_items_evented_update
        BEFORE UPDATE ON legal_review_items
        FOR EACH ROW
        EXECUTE FUNCTION enforce_legal_review_item_evented_update();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_legal_rule_human_verified_linkage()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            ok boolean;
        BEGIN
            IF TG_OP <> 'UPDATE' AND TG_OP <> 'INSERT' THEN
                RETURN NEW;
            END IF;

            -- Demotion / non-human_verified always allowed.
            IF NEW.verification_status IS DISTINCT FROM 'human_verified' THEN
                RETURN NEW;
            END IF;

            -- Only enforce when newly becoming human_verified or pins change while verified.
            IF TG_OP = 'UPDATE'
               AND OLD.verification_status = 'human_verified'
               AND NEW.verification_status = 'human_verified'
               AND OLD.document_version_id IS NOT DISTINCT FROM NEW.document_version_id
               AND OLD.reviewed_by IS NOT DISTINCT FROM NEW.reviewed_by
            THEN
                RETURN NEW;
            END IF;

            IF to_regclass('public.legal_review_items') IS NULL THEN
                RAISE EXCEPTION
                    'human_verified requires legal_review_items (migration 0012+)';
            END IF;

            SELECT EXISTS (
                SELECT 1
                  FROM legal_review_items i
                  JOIN legal_review_events e ON e.id = i.current_approved_event_id
                 WHERE i.legal_rule_id = NEW.id
                   AND i.workflow_state = 'approved'
                   AND i.invalidated = false
                   AND i.document_version_id IS NOT NULL
                   AND NEW.document_version_id IS NOT NULL
                   AND i.document_version_id = NEW.document_version_id
                   AND e.event_type = 'approved'
                   AND e.actor_kind = 'human_reviewer'
                   AND length(trim(e.actor_id)) > 0
                   AND NEW.review_status = 'approved'
                   AND NEW.reviewed_by IS NOT NULL
                   AND length(trim(NEW.reviewed_by)) > 0
                   AND NEW.reviewed_at IS NOT NULL
                   AND trim(NEW.reviewed_by) = trim(e.actor_id)
            ) INTO ok;

            IF NOT ok THEN
                RAISE EXCEPTION
                    'human_verified LegalRule requires linked approved non-invalidated '
                    'review event with matching document_version pin and human actor';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_legal_rules_human_verified_linkage
            ON legal_rules;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_legal_rules_human_verified_linkage
        BEFORE INSERT OR UPDATE ON legal_rules
        FOR EACH ROW
        EXECUTE FUNCTION enforce_legal_rule_human_verified_linkage();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_legal_rules_human_verified_linkage ON legal_rules"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_legal_rule_human_verified_linkage()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_legal_review_items_evented_update ON legal_review_items"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_legal_review_item_evented_update()")
