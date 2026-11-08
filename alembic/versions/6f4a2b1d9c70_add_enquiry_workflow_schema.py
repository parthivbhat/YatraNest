"""add structured enquiry fields, activity and reply records

Revision ID: 6f4a2b1d9c70
Revises: 9cca1aa0f200
Create Date: 2026-11-06
"""

from alembic import op
import sqlalchemy as sa


revision = "6f4a2b1d9c70"
down_revision = "9cca1aa0f200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("enquiries", sa.Column("travel_start_date", sa.Date(), nullable=True))
    op.add_column("enquiries", sa.Column("travel_end_date", sa.Date(), nullable=True))
    op.add_column("enquiries", sa.Column("travel_dates_flexible", sa.Boolean(), nullable=True))
    op.add_column("enquiries", sa.Column("adults", sa.Integer(), nullable=True))
    op.add_column("enquiries", sa.Column("children", sa.Integer(), nullable=True))
    op.add_column("enquiries", sa.Column("infants", sa.Integer(), nullable=True))
    op.add_column("enquiries", sa.Column("budget_amount_inr", sa.Numeric(12, 2), nullable=True))
    op.add_column("enquiries", sa.Column("response_notice_version", sa.String(length=40), nullable=True))
    op.add_column(
        "enquiries",
        sa.Column("response_notice_accepted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_enquiries_travel_dates_paired",
        "enquiries",
        "(travel_start_date IS NULL AND travel_end_date IS NULL) OR "
        "(travel_start_date IS NOT NULL AND travel_end_date IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_enquiries_travel_dates_ordered",
        "enquiries",
        "travel_start_date IS NULL OR travel_start_date <= travel_end_date",
    )
    op.create_check_constraint(
        "ck_enquiries_travel_flexibility_known",
        "enquiries",
        "travel_start_date IS NULL OR travel_dates_flexible IS NOT NULL",
    )
    for field in ("adults", "children", "infants"):
        op.create_check_constraint(
            f"ck_enquiries_{field}_nonnegative",
            "enquiries",
            f"{field} IS NULL OR {field} >= 0",
        )
    op.create_check_constraint(
        "ck_enquiries_budget_nonnegative",
        "enquiries",
        "budget_amount_inr IS NULL OR budget_amount_inr >= 0",
    )

    op.create_table(
        "enquiry_activities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enquiry_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("from_status", sa.String(length=30), nullable=True),
        sa.Column("to_status", sa.String(length=30), nullable=True),
        sa.Column("from_assigned_to_id", sa.Uuid(), nullable=True),
        sa.Column("to_assigned_to_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('status_changed', 'assignment_changed')",
            name="ck_enquiry_activities_event_type",
        ),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status IN ('new', 'in_progress', 'waiting_customer', 'closed')",
            name="ck_enquiry_activities_from_status",
        ),
        sa.CheckConstraint(
            "to_status IS NULL OR to_status IN ('new', 'in_progress', 'waiting_customer', 'closed')",
            name="ck_enquiry_activities_to_status",
        ),
        sa.CheckConstraint(
            "(event_type = 'status_changed' AND to_status IS NOT NULL) OR "
            "(event_type = 'assignment_changed' AND "
            "from_assigned_to_id IS DISTINCT FROM to_assigned_to_id)",
            name="ck_enquiry_activities_event_payload",
        ),
        sa.ForeignKeyConstraint(["enquiry_id"], ["enquiries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["from_assigned_to_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["to_assigned_to_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_enquiry_activities_enquiry_created", "enquiry_activities", ["enquiry_id", "created_at"]
    )

    op.create_table(
        "enquiry_replies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enquiry_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("approved_by_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manually_sent_by_id", sa.Uuid(), nullable=True),
        sa.Column("manually_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_reply_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'manually_sent')",
            name="ck_enquiry_replies_status",
        ),
        sa.CheckConstraint(
            "(status = 'draft' AND approved_at IS NULL AND manually_sent_at IS NULL) OR "
            "(status = 'approved' AND approved_at IS NOT NULL AND manually_sent_at IS NULL) OR "
            "(status = 'manually_sent' AND approved_at IS NOT NULL AND manually_sent_at IS NOT NULL)",
            name="ck_enquiry_replies_state_timestamps",
        ),
        sa.ForeignKeyConstraint(["enquiry_id"], ["enquiries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approved_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["manually_sent_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["supersedes_reply_id"], ["enquiry_replies.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_enquiry_replies_enquiry_created", "enquiry_replies", ["enquiry_id", "created_at"])
    op.create_index("ix_enquiry_replies_status_created", "enquiry_replies", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_enquiry_replies_status_created", table_name="enquiry_replies")
    op.drop_index("ix_enquiry_replies_enquiry_created", table_name="enquiry_replies")
    op.drop_table("enquiry_replies")
    op.drop_index("ix_enquiry_activities_enquiry_created", table_name="enquiry_activities")
    op.drop_table("enquiry_activities")
    for constraint in (
        "ck_enquiries_budget_nonnegative",
        "ck_enquiries_infants_nonnegative",
        "ck_enquiries_children_nonnegative",
        "ck_enquiries_adults_nonnegative",
        "ck_enquiries_travel_flexibility_known",
        "ck_enquiries_travel_dates_ordered",
        "ck_enquiries_travel_dates_paired",
    ):
        op.drop_constraint(constraint, "enquiries", type_="check")
    for column in (
        "response_notice_accepted_at",
        "response_notice_version",
        "budget_amount_inr",
        "infants",
        "children",
        "adults",
        "travel_dates_flexible",
        "travel_end_date",
        "travel_start_date",
    ):
        op.drop_column("enquiries", column)
