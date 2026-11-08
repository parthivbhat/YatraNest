"""Add nullable generation attempt fencing token to AI suggestions.

Revision ID: 7a4e2d9c1f63
Revises: e18bf6c4a920
Create Date: 2026-11-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "7a4e2d9c1f63"
down_revision = "e18bf6c4a920"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_suggestions",
        sa.Column("generation_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_suggestions", "generation_attempt_id")
