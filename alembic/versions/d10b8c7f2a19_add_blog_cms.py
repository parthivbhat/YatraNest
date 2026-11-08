"""Add the minimal server-rendered blog CMS.

Revision ID: d10b8c7f2a19
Revises: c08f6a1b2d34
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d10b8c7f2a19"
down_revision = "c08f6a1b2d34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blog_posts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("slug", sa.String(180), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("cover_image_path", sa.String(512), nullable=True),
        sa.Column("cover_image_alt", sa.String(200), nullable=True),
        sa.Column("seo_title", sa.String(160), nullable=True),
        sa.Column("seo_description", sa.String(320), nullable=True),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("slug", name="uq_blog_posts_slug"),
        sa.CheckConstraint("status IN ('draft', 'published')", name="ck_blog_posts_status"),
        sa.CheckConstraint(
            "(status = 'draft' AND published_at IS NULL) OR "
            "(status = 'published' AND published_at IS NOT NULL)",
            name="ck_blog_posts_publication_metadata",
        ),
        sa.CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="ck_blog_posts_slug_format"),
    )
    op.create_index("ix_blog_posts_status_published", "blog_posts", ["status", "published_at"])
    op.create_index("ix_blog_posts_category_status", "blog_posts", ["category", "status"])
    op.create_table(
        "blog_post_packages",
        sa.Column("blog_post_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("package_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["blog_post_id"], ["blog_posts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["package_id"], ["packages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("blog_post_id", "package_id", name="pk_blog_post_packages"),
    )
    op.create_index("ix_blog_post_packages_package_id", "blog_post_packages", ["package_id"])


def downgrade() -> None:
    op.drop_index("ix_blog_post_packages_package_id", table_name="blog_post_packages")
    op.drop_table("blog_post_packages")
    op.drop_index("ix_blog_posts_category_status", table_name="blog_posts")
    op.drop_index("ix_blog_posts_status_published", table_name="blog_posts")
    op.drop_table("blog_posts")
