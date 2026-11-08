"""Add staff quotation snapshots.

Revision ID: b6d3c2a1f908
Revises: 7a4e2d9c1f63
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b6d3c2a1f908"
down_revision = "7a4e2d9c1f63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quotations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("enquiry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("package_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("package_price_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("package_code_snapshot", sa.String(32), nullable=True),
        sa.Column("package_title_snapshot", sa.String(240), nullable=False),
        sa.Column("customer_name_snapshot", sa.String(160), nullable=False),
        sa.Column("travel_start_date", sa.Date(), nullable=True),
        sa.Column("travel_end_date", sa.Date(), nullable=True),
        sa.Column("adults", sa.Integer(), nullable=False),
        sa.Column("children", sa.Integer(), server_default="0", nullable=False),
        sa.Column("infants", sa.Integer(), server_default="0", nullable=False),
        sa.Column("accommodation", sa.String(160), nullable=False),
        sa.Column("currency", sa.String(3), server_default="INR", nullable=False),
        sa.Column("price_source", sa.String(20), nullable=False),
        sa.Column("price_basis", sa.String(80), server_default="per person, twin sharing", nullable=False),
        sa.Column("adult_unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("child_unit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("infant_unit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("staff_price_confirmed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("gst_rate_percent", sa.Numeric(5, 2), nullable=True),
        sa.Column("subtotal_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("gst_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("total_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("inclusions", sa.Text(), nullable=True),
        sa.Column("exclusions", sa.Text(), nullable=True),
        sa.Column("payment_terms", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("share_token_hash", sa.String(64), nullable=True),
        sa.Column("share_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["enquiry_id"], ["enquiries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["package_id"], ["packages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["package_price_id"], ["package_prices.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approved_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("status IN ('draft', 'approved')", name="ck_quotations_status"),
        sa.CheckConstraint(
            "(status = 'draft' AND approved_at IS NULL) OR (status = 'approved' AND approved_at IS NOT NULL)",
            name="ck_quotations_approval_metadata",
        ),
        sa.CheckConstraint("adults >= 1 AND adults <= 100", name="ck_quotations_adults_range"),
        sa.CheckConstraint("children >= 0 AND children <= 100", name="ck_quotations_children_range"),
        sa.CheckConstraint("infants >= 0 AND infants <= 100", name="ck_quotations_infants_range"),
        sa.CheckConstraint("adult_unit_price >= 0", name="ck_quotations_adult_price_nonnegative"),
        sa.CheckConstraint("child_unit_price IS NULL OR child_unit_price >= 0", name="ck_quotations_child_price_nonnegative"),
        sa.CheckConstraint("infant_unit_price IS NULL OR infant_unit_price >= 0", name="ck_quotations_infant_price_nonnegative"),
        sa.CheckConstraint("gst_rate_percent IS NULL OR gst_rate_percent BETWEEN 0 AND 100", name="ck_quotations_gst_range"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_quotations_currency_code"),
        sa.CheckConstraint("price_source IN ('package_price', 'manual')", name="ck_quotations_price_source"),
        sa.CheckConstraint(
            "(travel_start_date IS NULL AND travel_end_date IS NULL) OR "
            "(travel_start_date IS NOT NULL AND travel_end_date IS NOT NULL AND travel_start_date <= travel_end_date)",
            name="ck_quotations_travel_dates",
        ),
        sa.CheckConstraint(
            "total_amount IS NULL OR (subtotal_amount IS NOT NULL AND "
            "((gst_rate_percent IS NULL AND gst_amount IS NULL) OR "
            "(gst_rate_percent IS NOT NULL AND gst_amount IS NOT NULL)))",
            name="ck_quotations_total_components",
        ),
    )
    op.create_index("ix_quotations_enquiry_created", "quotations", ["enquiry_id", "created_at"])
    op.create_index("uq_quotations_share_token_hash", "quotations", ["share_token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_quotations_share_token_hash", table_name="quotations")
    op.drop_index("ix_quotations_enquiry_created", table_name="quotations")
    op.drop_table("quotations")
