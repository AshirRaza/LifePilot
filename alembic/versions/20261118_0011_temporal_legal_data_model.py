"""Add temporal lifecycle metadata on document_versions and curated legal_rules table.

Revision ID: 20261118_0011
Revises: 20261113_0010
Create Date: 2026-11-18

Phase 10B.1: Temporal Legal Data Model foundation.
- Additive, reversible schema changes only.
- Existing document_versions rows receive legal_status='unknown' (never treated
  as in_force or human-verified by default).
- Preserves documents, document_versions, document_chunks, and embeddings.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261118_0011"
down_revision: Union[str, Sequence[str], None] = "20261113_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DOC_LEGAL_STATUSES_SQL = (
    "'unknown', 'in_force', 'historical', 'superseded', "
    "'future_pending', 'proposed', 'withdrawn', 'archived', 'transitional'"
)
_REQ_CATEGORIES_SQL = (
    "'eligibility', 'required_documents', 'application_process', "
    "'financial_or_sponsorship', 'fees_costs', 'conditions_duration', "
    "'language_requirements', 'health_character', 'dependents', "
    "'work_rights', 'processing_times', 'official_provenance'"
)
_RULE_TYPES_SQL = (
    "'salary_threshold', 'maintenance_funds', 'application_fee', 'surcharge', "
    "'income_threshold', 'language_level', 'work_hours_limit', 'duration_limit', "
    "'residence_period', 'age_limit', 'eligibility_condition', 'exemption', "
    "'transitional_rule', 'document_requirement', 'process_step', 'other'"
)
_UNITS_SQL = (
    "'yearly', 'monthly', 'weekly', 'daily', 'hourly', 'one_time', "
    "'hours_per_week', 'days', 'months', 'years', 'percent', 'points', "
    "'count', 'other'"
)
_EFF_STATUSES_SQL = (
    "'unknown', 'partially_known', 'known_bounded', 'known_open_ended', "
    "'announced_future', 'superseded', 'expired'"
)
_COHORT_CATEGORIES_SQL = (
    "'general', 'transitional', 'age_based', 'qualification_based', "
    "'new_entrant', 'occupation_or_sector', 'location_based', "
    "'sponsorship_or_route_history', 'exemption', 'other'"
)
_REVIEW_STATUSES_SQL = (
    "'unreviewed', 'pending_review', 'approved', 'rejected', "
    "'clarification_requested', 'stale_pending_review'"
)
_VERIFICATION_STATUSES_SQL = (
    "'unverified', 'machine_extracted_unverified', 'human_verified', "
    "'disputed', 'potentially_outdated'"
)


def upgrade() -> None:
    # 1. Additive source-lifecycle & observation metadata on document_versions.
    op.add_column(
        "document_versions",
        sa.Column("http_last_modified", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "document_versions",
        sa.Column("http_etag", sa.Text(), nullable=True),
    )
    op.add_column(
        "document_versions",
        sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "document_versions",
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "document_versions",
        sa.Column(
            "legal_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
    )
    op.add_column(
        "document_versions",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "document_versions",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "document_versions",
        sa.Column("superseded_by_version_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "document_versions",
        sa.Column("temporal_notes", sa.Text(), nullable=True),
    )

    op.create_foreign_key(
        "fk_document_versions_superseded_by_version_id",
        "document_versions",
        "document_versions",
        ["superseded_by_version_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_document_versions_legal_status",
        "document_versions",
        f"legal_status IN ({_DOC_LEGAL_STATUSES_SQL})",
    )
    op.create_check_constraint(
        "ck_document_versions_not_self_superseded",
        "document_versions",
        "superseded_by_version_id IS NULL OR superseded_by_version_id <> id",
    )
    op.create_check_constraint(
        "ck_document_versions_http_etag_nonempty",
        "document_versions",
        "http_etag IS NULL OR length(trim(http_etag)) > 0",
    )
    op.create_index(
        "ix_document_versions_legal_status",
        "document_versions",
        ["legal_status"],
    )
    op.create_index(
        "ix_document_versions_superseded_by_version_id",
        "document_versions",
        ["superseded_by_version_id"],
    )
    op.create_index(
        "ix_document_versions_source_updated_at",
        "document_versions",
        ["source_updated_at"],
    )

    # 2. Curated, temporally scoped legal_rules table.
    op.create_table(
        "legal_rules",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("rule_key", sa.Text(), nullable=False),
        sa.Column(
            "rule_revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("supersedes_rule_id", sa.BigInteger(), nullable=True),
        sa.Column("jurisdiction_id", sa.BigInteger(), nullable=False),
        sa.Column("jurisdiction_code", sa.Text(), nullable=False),
        sa.Column("pathway_id", sa.Text(), nullable=True),
        sa.Column("requirement_category", sa.Text(), nullable=False),
        sa.Column("requirement_type", sa.Text(), nullable=False),
        sa.Column("numeric_value", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column(
            "numeric_max_value", sa.Numeric(precision=18, scale=4), nullable=True
        ),
        sa.Column("numeric_comparator", sa.Text(), nullable=True),
        sa.Column("currency_code", postgresql.CHAR(length=3), nullable=True),
        sa.Column("amount_unit", sa.Text(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "effective_from_inclusive",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "effective_to_inclusive",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "effective_date_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
        sa.Column(
            "cohort_key",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'general'"),
        ),
        sa.Column(
            "cohort_category",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'general'"),
        ),
        sa.Column(
            "is_transitional",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "cohort_conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("rule_summary", sa.Text(), nullable=False),
        sa.Column("conditions_text", sa.Text(), nullable=True),
        sa.Column("source_id", sa.BigInteger(), nullable=True),
        sa.Column("document_id", sa.BigInteger(), nullable=True),
        sa.Column("document_version_id", sa.BigInteger(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("supporting_excerpt", sa.Text(), nullable=True),
        sa.Column(
            "review_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'unreviewed'"),
        ),
        sa.Column(
            "verification_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'unverified'"),
        ),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_notes", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "rule_key", "rule_revision", name="uq_legal_rules_key_revision"
        ),
        sa.ForeignKeyConstraint(["supersedes_rule_id"], ["legal_rules.id"]),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdictions.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"]),
        sa.CheckConstraint(
            "rule_key ~ '^[a-z0-9][a-z0-9_.-]*$'",
            name="ck_legal_rules_rule_key",
        ),
        sa.CheckConstraint("rule_revision >= 1", name="ck_legal_rules_rule_revision"),
        sa.CheckConstraint(
            "supersedes_rule_id IS NULL OR supersedes_rule_id <> id",
            name="ck_legal_rules_not_self_superseded",
        ),
        sa.CheckConstraint(
            "length(trim(jurisdiction_code)) > 0",
            name="ck_legal_rules_jurisdiction_code_nonempty",
        ),
        sa.CheckConstraint(
            "pathway_id IS NULL OR pathway_id ~ '^[a-z][a-z0-9_]*$'",
            name="ck_legal_rules_pathway_id",
        ),
        sa.CheckConstraint(
            f"requirement_category IN ({_REQ_CATEGORIES_SQL})",
            name="ck_legal_rules_requirement_category",
        ),
        sa.CheckConstraint(
            f"requirement_type IN ({_RULE_TYPES_SQL})",
            name="ck_legal_rules_requirement_type",
        ),
        sa.CheckConstraint(
            "numeric_value IS NULL OR numeric_value >= 0",
            name="ck_legal_rules_numeric_nonnegative",
        ),
        sa.CheckConstraint(
            "numeric_comparator IS NULL OR numeric_comparator IN ('gte', 'gt', 'lte', 'lt', 'eq')",
            name="ck_legal_rules_numeric_comparator",
        ),
        sa.CheckConstraint(
            "numeric_max_value IS NULL OR (numeric_value IS NOT NULL AND numeric_max_value >= numeric_value)",
            name="ck_legal_rules_numeric_range",
        ),
        sa.CheckConstraint(
            "currency_code IS NULL OR (currency_code ~ '^[A-Z]{3}$' AND numeric_value IS NOT NULL)",
            name="ck_legal_rules_currency_code",
        ),
        sa.CheckConstraint(
            f"amount_unit IS NULL OR (amount_unit IN ({_UNITS_SQL}) AND numeric_value IS NOT NULL)",
            name="ck_legal_rules_amount_unit",
        ),
        sa.CheckConstraint(
            f"effective_date_status IN ({_EFF_STATUSES_SQL})",
            name="ck_legal_rules_effective_date_status",
        ),
        sa.CheckConstraint(
            "effective_date_status <> 'unknown' OR (effective_from IS NULL AND effective_to IS NULL)",
            name="ck_legal_rules_unknown_effective_dates",
        ),
        sa.CheckConstraint(
            "effective_date_status NOT IN ('known_bounded', 'known_open_ended', 'announced_future') "
            "OR effective_from IS NOT NULL",
            name="ck_legal_rules_known_from_date",
        ),
        sa.CheckConstraint(
            "effective_date_status <> 'known_bounded' OR effective_to IS NOT NULL",
            name="ck_legal_rules_known_to_date",
        ),
        sa.CheckConstraint(
            "effective_from IS NULL OR effective_to IS NULL OR ("
            "(effective_from_inclusive AND effective_to_inclusive AND effective_to >= effective_from) "
            "OR ((NOT effective_from_inclusive OR NOT effective_to_inclusive) AND effective_to > effective_from)"
            ")",
            name="ck_legal_rules_effective_bounds_order",
        ),
        sa.CheckConstraint(
            "cohort_key ~ '^[a-z0-9][a-z0-9_.-]*$'",
            name="ck_legal_rules_cohort_key",
        ),
        sa.CheckConstraint(
            f"cohort_category IN ({_COHORT_CATEGORIES_SQL})",
            name="ck_legal_rules_cohort_category",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(cohort_conditions) = 'object'",
            name="ck_legal_rules_cohort_conditions_object",
        ),
        sa.CheckConstraint(
            "length(trim(rule_summary)) > 0",
            name="ck_legal_rules_rule_summary_nonempty",
        ),
        sa.CheckConstraint(
            "document_version_id IS NOT NULL OR document_id IS NOT NULL OR source_id IS NOT NULL",
            name="ck_legal_rules_requires_source_provenance",
        ),
        sa.CheckConstraint(
            f"review_status IN ({_REVIEW_STATUSES_SQL})",
            name="ck_legal_rules_review_status",
        ),
        sa.CheckConstraint(
            f"verification_status IN ({_VERIFICATION_STATUSES_SQL})",
            name="ck_legal_rules_verification_status",
        ),
        sa.CheckConstraint(
            "verification_status <> 'human_verified' OR ("
            "review_status = 'approved' "
            "AND reviewed_by IS NOT NULL "
            "AND length(trim(reviewed_by)) > 0 "
            "AND reviewed_at IS NOT NULL "
            "AND document_version_id IS NOT NULL"
            ")",
            name="ck_legal_rules_human_verified_requires_audit",
        ),
    )

    op.create_index(
        "ix_legal_rules_lookup",
        "legal_rules",
        [
            "jurisdiction_code",
            "pathway_id",
            "requirement_category",
            "effective_from",
            "effective_to",
        ],
    )
    op.create_index("ix_legal_rules_rule_key", "legal_rules", ["rule_key"])
    op.create_index(
        "ix_legal_rules_document_version_id",
        "legal_rules",
        ["document_version_id"],
    )
    op.create_index("ix_legal_rules_document_id", "legal_rules", ["document_id"])
    op.create_index("ix_legal_rules_source_id", "legal_rules", ["source_id"])
    op.create_index(
        "ix_legal_rules_verification_status",
        "legal_rules",
        ["verification_status", "review_status"],
    )
    op.create_index(
        "ix_legal_rules_effective_dates",
        "legal_rules",
        ["effective_from", "effective_to"],
    )
    op.create_index(
        "ix_legal_rules_cohort",
        "legal_rules",
        ["pathway_id", "cohort_key"],
    )

    # Keep denormalized jurisdiction_code aligned with jurisdictions.code.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION legal_rules_enforce_jurisdiction_code()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            expected_code text;
        BEGIN
            SELECT code INTO expected_code
            FROM jurisdictions
            WHERE id = NEW.jurisdiction_id;
            IF expected_code IS NULL THEN
                RAISE EXCEPTION
                    'legal_rules.jurisdiction_id % does not exist',
                    NEW.jurisdiction_id;
            END IF;
            IF NEW.jurisdiction_code IS DISTINCT FROM expected_code THEN
                RAISE EXCEPTION
                    'legal_rules.jurisdiction_code % does not match jurisdictions.code % for id %',
                    NEW.jurisdiction_code, expected_code, NEW.jurisdiction_id;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_legal_rules_jurisdiction_code
        BEFORE INSERT OR UPDATE OF jurisdiction_id, jurisdiction_code
        ON legal_rules
        FOR EACH ROW
        EXECUTE FUNCTION legal_rules_enforce_jurisdiction_code();
        """
    )

    # Ensure document_id and document_version_id cannot point to different documents.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION legal_rules_enforce_provenance_consistency()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            version_doc_id bigint;
        BEGIN
            IF NEW.document_version_id IS NOT NULL THEN
                SELECT document_id INTO version_doc_id
                FROM document_versions
                WHERE id = NEW.document_version_id;
                IF version_doc_id IS NULL THEN
                    RAISE EXCEPTION
                        'legal_rules.document_version_id % does not exist',
                        NEW.document_version_id;
                END IF;
                IF NEW.document_id IS NOT NULL AND NEW.document_id IS DISTINCT FROM version_doc_id THEN
                    RAISE EXCEPTION
                        'legal_rules.document_id % does not match document_versions.document_id % for version %',
                        NEW.document_id, version_doc_id, NEW.document_version_id;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_legal_rules_provenance_consistency
        BEFORE INSERT OR UPDATE OF document_id, document_version_id
        ON legal_rules
        FOR EACH ROW
        EXECUTE FUNCTION legal_rules_enforce_provenance_consistency();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_legal_rules_provenance_consistency ON legal_rules"
    )
    op.execute("DROP FUNCTION IF EXISTS legal_rules_enforce_provenance_consistency()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_legal_rules_jurisdiction_code ON legal_rules"
    )
    op.execute("DROP FUNCTION IF EXISTS legal_rules_enforce_jurisdiction_code()")

    op.drop_index("ix_legal_rules_cohort", table_name="legal_rules")
    op.drop_index("ix_legal_rules_effective_dates", table_name="legal_rules")
    op.drop_index("ix_legal_rules_verification_status", table_name="legal_rules")
    op.drop_index("ix_legal_rules_source_id", table_name="legal_rules")
    op.drop_index("ix_legal_rules_document_id", table_name="legal_rules")
    op.drop_index("ix_legal_rules_document_version_id", table_name="legal_rules")
    op.drop_index("ix_legal_rules_rule_key", table_name="legal_rules")
    op.drop_index("ix_legal_rules_lookup", table_name="legal_rules")
    op.drop_table("legal_rules")

    op.drop_index(
        "ix_document_versions_source_updated_at", table_name="document_versions"
    )
    op.drop_index(
        "ix_document_versions_superseded_by_version_id",
        table_name="document_versions",
    )
    op.drop_index(
        "ix_document_versions_legal_status", table_name="document_versions"
    )
    op.drop_constraint(
        "ck_document_versions_http_etag_nonempty",
        "document_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_document_versions_not_self_superseded",
        "document_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_document_versions_legal_status",
        "document_versions",
        type_="check",
    )
    op.drop_constraint(
        "fk_document_versions_superseded_by_version_id",
        "document_versions",
        type_="foreignkey",
    )

    op.drop_column("document_versions", "temporal_notes")
    op.drop_column("document_versions", "superseded_by_version_id")
    op.drop_column("document_versions", "superseded_at")
    op.drop_column("document_versions", "archived_at")
    op.drop_column("document_versions", "legal_status")
    op.drop_column("document_versions", "source_updated_at")
    op.drop_column("document_versions", "source_published_at")
    op.drop_column("document_versions", "http_etag")
    op.drop_column("document_versions", "http_last_modified")
