"""add versioned AI extraction review foundation

Revision ID: c37a8b6d21f4
Revises: a8149f2c6e35
Create Date: 2026-11-06

Existing rows retain NULL for the newly added lifecycle fields. This avoids
fabricating a generation or review state for legacy AI suggestion records.
Downgrade removes only this revision's new columns and event table.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c37a8b6d21f4"
down_revision = "a8149f2c6e35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_suggestions", sa.Column("generation_status", sa.String(length=20), nullable=True))
    op.add_column("ai_suggestions", sa.Column("accepted_extraction", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("ai_suggestions", sa.Column("extraction_review_status", sa.String(length=20), nullable=True))
    op.add_column("ai_suggestions", sa.Column("extraction_reviewed_by_id", sa.Uuid(), nullable=True))
    op.add_column("ai_suggestions", sa.Column("extraction_reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ai_suggestions", sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ai_suggestions", sa.Column("failure_code", sa.String(length=60), nullable=True))
    op.create_foreign_key(
        "fk_ai_suggestions_extraction_reviewed_by_id_users",
        "ai_suggestions",
        "users",
        ["extraction_reviewed_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_ai_suggestions_generation_status",
        "ai_suggestions",
        "generation_status IS NULL OR generation_status IN "
        "('pending', 'processing', 'completed', 'failed', 'unavailable')",
    )
    op.create_check_constraint(
        "ck_ai_suggestions_extraction_review_status",
        "ai_suggestions",
        "extraction_review_status IS NULL OR extraction_review_status IN "
        "('pending', 'accepted', 'corrected')",
    )
    op.create_check_constraint(
        "ck_ai_suggestions_extraction_review_metadata",
        "ai_suggestions",
        "(extraction_review_status IN ('accepted', 'corrected') AND "
        "extraction_reviewed_by_id IS NOT NULL AND extraction_reviewed_at IS NOT NULL) OR "
        "(extraction_review_status IS NULL OR extraction_review_status = 'pending') AND "
        "extraction_reviewed_by_id IS NULL AND extraction_reviewed_at IS NULL",
    )
    op.create_index("ix_ai_suggestions_generation_status", "ai_suggestions", ["generation_status"])

    op.create_table(
        "ai_extraction_review_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ai_suggestion_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("previous_extraction", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("accepted_extraction", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("action_type", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "action_type IN ('accepted', 'corrected')",
            name="ck_ai_extraction_review_events_action",
        ),
        sa.ForeignKeyConstraint(["ai_suggestion_id"], ["ai_suggestions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_extraction_review_events_suggestion_created",
        "ai_extraction_review_events",
        ["ai_suggestion_id", "created_at"],
    )

    op.add_column("enquiry_replies", sa.Column("source_ai_suggestion_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_enquiry_replies_source_ai_suggestion_id_ai_suggestions",
        "enquiry_replies",
        "ai_suggestions",
        ["source_ai_suggestion_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_enquiry_replies_source_ai_suggestion", "enquiry_replies", ["source_ai_suggestion_id"])


def downgrade() -> None:
    op.drop_index("ix_enquiry_replies_source_ai_suggestion", table_name="enquiry_replies")
    op.drop_constraint(
        "fk_enquiry_replies_source_ai_suggestion_id_ai_suggestions",
        "enquiry_replies",
        type_="foreignkey",
    )
    op.drop_column("enquiry_replies", "source_ai_suggestion_id")

    op.drop_index(
        "ix_ai_extraction_review_events_suggestion_created",
        table_name="ai_extraction_review_events",
    )
    op.drop_table("ai_extraction_review_events")

    op.drop_index("ix_ai_suggestions_generation_status", table_name="ai_suggestions")
    op.drop_constraint("ck_ai_suggestions_extraction_review_metadata", "ai_suggestions", type_="check")
    op.drop_constraint("ck_ai_suggestions_extraction_review_status", "ai_suggestions", type_="check")
    op.drop_constraint("ck_ai_suggestions_generation_status", "ai_suggestions", type_="check")
    op.drop_constraint(
        "fk_ai_suggestions_extraction_reviewed_by_id_users",
        "ai_suggestions",
        type_="foreignkey",
    )
    for column in (
        "failure_code",
        "generated_at",
        "extraction_reviewed_at",
        "extraction_reviewed_by_id",
        "extraction_review_status",
        "accepted_extraction",
        "generation_status",
    ):
        op.drop_column("ai_suggestions", column)
