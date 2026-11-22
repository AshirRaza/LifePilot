"""Phase 10D.5 — Block direct legal_review_events INSERT; gated writer function.

Revision ID: 20261122_0014
Revises: 20261122_0013
Create Date: 2026-11-22

Ordinary SQL sessions (including the usual application login) cannot INSERT
into legal_review_events unless the insert runs as role ``lifepilot_gov_writer``
via SECURITY DEFINER function ``lp_insert_legal_review_event``.

This does **not** prove human identity. It prevents forged approval *rows*
under ordinary application DML privileges. Superusers can still
``DISABLE TRIGGER USER`` (documented DBA break-glass).

Also hardens legal_review_items updates so ``approved`` requires
``current_approved_event_id`` to equal the latest event id and that event
to be ``event_type='approved'`` with ``actor_kind='human_reviewer'``.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "20261122_0014"
down_revision: Union[str, Sequence[str], None] = "20261122_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

GOV_WRITER_ROLE = "lifepilot_gov_writer"


def upgrade() -> None:
    # NOLOGIN role that owns the gated insert path. Idempotent for re-runs.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{GOV_WRITER_ROLE}') THEN
                CREATE ROLE {GOV_WRITER_ROLE} NOLOGIN;
            END IF;
        END
        $$;
        """
    )
    # Current migration user must be able to SET ROLE into the writer for
    # ownership transfer and function creation as that role when needed.
    op.execute(f"GRANT {GOV_WRITER_ROLE} TO CURRENT_USER")
    # Writer role must be able to INSERT while SECURITY DEFINER runs as that role.
    # Table ownership stays with the migration/app owner; only INSERT is gated.
    op.execute(
        f"""
        GRANT SELECT, INSERT ON TABLE legal_review_events TO {GOV_WRITER_ROLE};
        GRANT USAGE, SELECT ON SEQUENCE legal_review_events_id_seq TO {GOV_WRITER_ROLE};
        GRANT SELECT ON TABLE legal_review_items TO {GOV_WRITER_ROLE};
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_legal_review_event_insert_gate()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            -- Only SECURITY DEFINER inserts running as lifepilot_gov_writer pass.
            -- Direct INSERT by the ordinary app login is rejected even if GRANT
            -- INSERT was mistakenly restored. Superuser DISABLE TRIGGER remains
            -- the documented recovery path (not proof of human identity).
            IF current_user IS DISTINCT FROM 'lifepilot_gov_writer' THEN
                RAISE EXCEPTION
                    'direct INSERT into legal_review_events is forbidden; '
                    'use lp_insert_legal_review_event() (current_user=%)',
                    current_user;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_legal_review_events_insert_gate
            ON legal_review_events;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_legal_review_events_insert_gate
        BEFORE INSERT ON legal_review_events
        FOR EACH ROW
        EXECUTE FUNCTION enforce_legal_review_event_insert_gate();
        """
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION lp_insert_legal_review_event(
            p_review_item_id bigint,
            p_event_type text,
            p_from_workflow_state text,
            p_to_workflow_state text,
            p_occurred_at timestamptz,
            p_actor_id text,
            p_actor_kind text,
            p_rationale text,
            p_evidence_fingerprint text,
            p_document_version_id bigint,
            p_metadata jsonb
        )
        RETURNS bigint
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public
        AS $$
        DECLARE
            new_id bigint;
        BEGIN
            -- Run as governance writer so the INSERT gate passes.
            INSERT INTO legal_review_events (
                review_item_id,
                event_type,
                from_workflow_state,
                to_workflow_state,
                occurred_at,
                actor_id,
                actor_kind,
                rationale,
                evidence_fingerprint,
                document_version_id,
                metadata
            ) VALUES (
                p_review_item_id,
                p_event_type,
                p_from_workflow_state,
                p_to_workflow_state,
                COALESCE(p_occurred_at, NOW()),
                p_actor_id,
                p_actor_kind,
                p_rationale,
                p_evidence_fingerprint,
                p_document_version_id,
                COALESCE(p_metadata, '{{}}'::jsonb)
            )
            RETURNING id INTO new_id;
            RETURN new_id;
        END;
        $$;
        """
    )
    # Ensure the function runs with gov writer privileges regardless of owner.
    op.execute(
        f"ALTER FUNCTION lp_insert_legal_review_event("
        f"bigint, text, text, text, timestamptz, text, text, text, text, bigint, jsonb"
        f") OWNER TO {GOV_WRITER_ROLE}"
    )
    op.execute(
        """
        GRANT EXECUTE ON FUNCTION lp_insert_legal_review_event(
            bigint, text, text, text, timestamptz, text, text, text, text, bigint, jsonb
        ) TO PUBLIC;
        """
    )

    # Harden item updates: approved state must point at a real human approval event.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_legal_review_item_evented_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            latest_to_state text;
            latest_event_id bigint;
            latest_event_type text;
            latest_actor_kind text;
        BEGIN
            IF TG_OP <> 'UPDATE' THEN
                RETURN NEW;
            END IF;

            IF (OLD.workflow_state IS NOT DISTINCT FROM NEW.workflow_state)
               AND (OLD.invalidated IS NOT DISTINCT FROM NEW.invalidated)
               AND (OLD.current_approved_event_id IS NOT DISTINCT FROM NEW.current_approved_event_id)
               AND (OLD.row_version IS NOT DISTINCT FROM NEW.row_version)
               AND (OLD.assigned_actor_id IS NOT DISTINCT FROM NEW.assigned_actor_id)
               AND (OLD.invalidation_reason IS NOT DISTINCT FROM NEW.invalidation_reason)
            THEN
                RETURN NEW;
            END IF;

            SELECT e.id, e.to_workflow_state, e.event_type, e.actor_kind
              INTO latest_event_id, latest_to_state, latest_event_type, latest_actor_kind
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

            IF NEW.row_version < OLD.row_version THEN
                RAISE EXCEPTION 'legal_review_items.row_version cannot decrease';
            END IF;

            IF NEW.workflow_state = 'approved' THEN
                IF NEW.current_approved_event_id IS NULL
                   OR NEW.current_approved_event_id IS DISTINCT FROM latest_event_id THEN
                    RAISE EXCEPTION
                        'approved review item requires current_approved_event_id '
                        'to equal the latest event id';
                END IF;
                IF latest_event_type IS DISTINCT FROM 'approved'
                   OR latest_actor_kind IS DISTINCT FROM 'human_reviewer' THEN
                    RAISE EXCEPTION
                        'approved review item requires latest event to be '
                        'approved by human_reviewer';
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )


def downgrade() -> None:
    # Restore 0013 item-trigger body (without approved-event hardening).
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

            IF NEW.row_version < OLD.row_version THEN
                RAISE EXCEPTION 'legal_review_items.row_version cannot decrease';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS lp_insert_legal_review_event("
        "bigint, text, text, text, timestamptz, text, text, text, text, bigint, jsonb)"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_legal_review_events_insert_gate ON legal_review_events"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_legal_review_event_insert_gate()")
    # Leave role in place (may be granted elsewhere); safe no-op if unused.
