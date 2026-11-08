"""add one-use public form nonces

Revision ID: a8149f2c6e35
Revises: 6f4a2b1d9c70
Create Date: 2026-11-06
"""

from alembic import op
import sqlalchemy as sa


revision = "a8149f2c6e35"
down_revision = "6f4a2b1d9c70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "public_form_nonces",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["package_id"], ["packages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_public_form_nonces_expires_at", "public_form_nonces", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_public_form_nonces_expires_at", table_name="public_form_nonces")
    op.drop_table("public_form_nonces")
