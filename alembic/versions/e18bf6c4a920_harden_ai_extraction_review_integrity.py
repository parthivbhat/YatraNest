"""harden AI extraction lifecycle and review-event integrity

Revision ID: e18bf6c4a920
Revises: c37a8b6d21f4
Create Date: 2026-11-06

This revision leaves existing NULL lifecycle values unchanged. Defaults apply
only to future inserts. The trigger prevents UPDATE/DELETE while preserving
ordinary INSERT operations. Downgrade removes only this revision's additions.
"""

from alembic import op


revision = "e18bf6c4a920"
down_revision = "c37a8b6d21f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Setting defaults after the columns already exist does not backfill legacy rows.
    op.alter_column("ai_suggestions", "generation_status", server_default="pending")
    op.alter_column("ai_suggestions", "extraction_review_status", server_default="pending")
    op.create_check_constraint(
        "ck_ai_suggestions_failure_code",
        "ai_suggestions",
        "failure_code IS NULL OR failure_code IN "
        "('provider_unavailable', 'provider_timeout', 'invalid_provider_response', "
        "'validation_failed', 'generation_failed')",
    )
    op.drop_constraint(
        "ai_extraction_review_events_actor_id_fkey",
        "ai_extraction_review_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_ai_extraction_review_events_actor_id_users",
        "ai_extraction_review_events",
        "users",
        ["actor_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        CREATE FUNCTION reject_ai_extraction_review_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'AI extraction review events are immutable'
                USING ERRCODE = '55000';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ai_extraction_review_events_immutable
        BEFORE UPDATE OR DELETE ON ai_extraction_review_events
        FOR EACH ROW EXECUTE FUNCTION reject_ai_extraction_review_event_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_ai_extraction_review_events_immutable ON ai_extraction_review_events")
    op.execute("DROP FUNCTION IF EXISTS reject_ai_extraction_review_event_mutation()")
    op.drop_constraint("ck_ai_suggestions_failure_code", "ai_suggestions", type_="check")
    op.drop_constraint(
        "fk_ai_extraction_review_events_actor_id_users",
        "ai_extraction_review_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "ai_extraction_review_events_actor_id_fkey",
        "ai_extraction_review_events",
        "users",
        ["actor_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column("ai_suggestions", "extraction_review_status", server_default=None)
    op.alter_column("ai_suggestions", "generation_status", server_default=None)
