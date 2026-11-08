"""Allow pre-GST totals when the GST rate is unspecified.

Revision ID: c08f6a1b2d34
Revises: b6d3c2a1f908
"""

from alembic import op
import sqlalchemy as sa


revision = "c08f6a1b2d34"
down_revision = "b6d3c2a1f908"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earlier development copies of the first additive revision included this
    # reference check; it conflicts with PackagePrice ON DELETE SET NULL.
    op.execute(sa.text(
        "ALTER TABLE quotations DROP CONSTRAINT IF EXISTS ck_quotations_price_source_reference"
    ))
    op.drop_constraint("ck_quotations_total_components", "quotations", type_="check")
    op.create_check_constraint(
        "ck_quotations_total_components",
        "quotations",
        "total_amount IS NULL OR (subtotal_amount IS NOT NULL AND "
        "((gst_rate_percent IS NULL AND gst_amount IS NULL) OR "
        "(gst_rate_percent IS NOT NULL AND gst_amount IS NOT NULL)))",
    )


def downgrade() -> None:
    op.drop_constraint("ck_quotations_total_components", "quotations", type_="check")
    op.create_check_constraint(
        "ck_quotations_total_components",
        "quotations",
        "total_amount IS NULL OR (subtotal_amount IS NOT NULL AND "
        "((gst_rate_percent IS NULL AND gst_amount IS NULL) OR "
        "(gst_rate_percent IS NOT NULL AND gst_amount IS NOT NULL)))",
    )
