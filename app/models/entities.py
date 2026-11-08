"""Relational models used by the tour operator platform."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Column,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import inspect

from app.models.base import Base, TimestampMixin


blog_post_packages = Table(
    "blog_post_packages",
    Base.metadata,
    Column("blog_post_id", Uuid(as_uuid=True), ForeignKey("blog_posts.id", ondelete="CASCADE"), primary_key=True),
    Column("package_id", Uuid(as_uuid=True), ForeignKey("packages.id", ondelete="CASCADE"), primary_key=True),
)
Index("ix_blog_post_packages_package_id", blog_post_packages.c.package_id)


class User(TimestampMixin, Base):
    """Staff account; password hashes are created by the staff account service."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'editor', 'consultant')", name="ck_users_role"),
        Index("uq_users_email_lower", func.lower(text("email")), unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="consultant", server_default="consultant")
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))

    packages_created: Mapped[list[Package]] = relationship(
        back_populates="created_by",
        foreign_keys="Package.created_by_id",
    )
    packages_updated: Mapped[list[Package]] = relationship(
        back_populates="updated_by",
        foreign_keys="Package.updated_by_id",
    )
    package_versions: Mapped[list[PackageVersion]] = relationship(back_populates="created_by")
    assigned_enquiries: Mapped[list[Enquiry]] = relationship(
        back_populates="assigned_to",
        foreign_keys="Enquiry.assigned_to_id",
    )
    enquiry_notes: Mapped[list[EnquiryNote]] = relationship(back_populates="author")
    enquiry_activities: Mapped[list[EnquiryActivity]] = relationship(
        back_populates="actor", foreign_keys="EnquiryActivity.actor_id"
    )
    enquiry_replies_authored: Mapped[list[EnquiryReply]] = relationship(
        back_populates="author", foreign_keys="EnquiryReply.author_id"
    )
    enquiry_replies_approved: Mapped[list[EnquiryReply]] = relationship(
        back_populates="approved_by", foreign_keys="EnquiryReply.approved_by_id"
    )
    enquiry_replies_marked_sent: Mapped[list[EnquiryReply]] = relationship(
        back_populates="manually_sent_by", foreign_keys="EnquiryReply.manually_sent_by_id"
    )
    reviewed_ai_suggestions: Mapped[list[AISuggestion]] = relationship(
        back_populates="reviewed_by", foreign_keys="AISuggestion.reviewed_by_id"
    )
    ai_extraction_reviews: Mapped[list[AIExtractionReviewEvent]] = relationship(back_populates="actor")
    sessions: Mapped[list[UserSession]] = relationship(back_populates="user", cascade="all, delete-orphan")
    package_drafts_created: Mapped[list[PackageDraft]] = relationship(
        back_populates="created_by",
        foreign_keys="PackageDraft.created_by_id",
    )
    package_drafts_updated: Mapped[list[PackageDraft]] = relationship(
        back_populates="updated_by",
        foreign_keys="PackageDraft.updated_by_id",
    )
    blog_posts_authored: Mapped[list[BlogPost]] = relationship(
        back_populates="author", foreign_keys="BlogPost.author_id"
    )


class Package(TimestampMixin, Base):
    """Editable package content and its current publication state."""

    __tablename__ = "packages"
    __table_args__ = (
        CheckConstraint("duration_days IS NULL OR duration_days > 0", name="ck_packages_duration_positive"),
        CheckConstraint("status IN ('draft', 'published', 'archived')", name="ck_packages_status"),
        Index("ix_packages_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    package_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    slug: Mapped[str | None] = mapped_column(String(180), nullable=True, unique=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    destinations: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hotel_category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    hotel_details: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    itinerary: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    inclusions: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    map_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", server_default="draft")
    scheduled_publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_by: Mapped[User | None] = relationship(back_populates="packages_created", foreign_keys=[created_by_id])
    updated_by: Mapped[User | None] = relationship(back_populates="packages_updated", foreign_keys=[updated_by_id])
    prices: Mapped[list[PackagePrice]] = relationship(
        back_populates="package",
        cascade="all, delete-orphan",
        order_by="PackagePrice.start_month, PackagePrice.season_name",
    )
    versions: Mapped[list[PackageVersion]] = relationship(
        back_populates="package",
        order_by="PackageVersion.version_number",
        passive_deletes="all",
    )
    enquiries: Mapped[list[Enquiry]] = relationship(back_populates="package")
    pending_draft: Mapped[PackageDraft | None] = relationship(
        back_populates="package",
        cascade="all, delete-orphan",
        uselist=False,
    )

    def to_snapshot(self) -> dict[str, Any]:
        """Return the complete package state, including every seasonal price."""
        return {
            "package_code": self.package_code,
            "slug": self.slug,
            "title": self.title,
            "summary": self.summary,
            "description": self.description,
            "destinations": self.destinations,
            "duration_days": self.duration_days,
            "hotel_category": self.hotel_category,
            "hotel_details": self.hotel_details,
            "itinerary": self.itinerary,
            "inclusions": self.inclusions,
            "map_url": self.map_url,
            "status": self.status,
            "scheduled_publish_at": self.scheduled_publish_at.isoformat() if self.scheduled_publish_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "prices": [
                price.to_snapshot()
                for price in sorted(self.prices, key=lambda item: (item.start_month, item.end_month, item.season_name))
            ],
        }


class PackagePrice(TimestampMixin, Base):
    """A price valid during a recurring month window and optional absolute dates.

    When start_month is greater than end_month, the recurring season crosses the
    year boundary (for example, 10 through 3 means October through March).
    Absolute valid_from/valid_until bounds can further limit a season to specific
    dates. Both absolute bounds must be supplied together.
    """

    __tablename__ = "package_prices"
    __table_args__ = (
        CheckConstraint("start_month BETWEEN 1 AND 12", name="ck_package_prices_start_month"),
        CheckConstraint("end_month BETWEEN 1 AND 12", name="ck_package_prices_end_month"),
        CheckConstraint("price_per_person >= 0", name="ck_package_prices_nonnegative"),
        CheckConstraint(
            "(valid_from IS NULL AND valid_until IS NULL) OR "
            "(valid_from IS NOT NULL AND valid_until IS NOT NULL AND valid_from <= valid_until)",
            name="ck_package_prices_valid_date_range",
        ),
        UniqueConstraint(
            "package_id",
            "season_name",
            "start_month",
            "end_month",
            name="uq_package_prices_season_window",
        ),
        Index("ix_package_prices_package_months", "package_id", "start_month", "end_month"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    package_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("packages.id", ondelete="CASCADE"), nullable=False
    )
    season_name: Mapped[str] = mapped_column(String(100), nullable=False)
    start_month: Mapped[int] = mapped_column(Integer, nullable=False)
    end_month: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    price_per_person: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR", server_default="INR")
    price_basis: Mapped[str] = mapped_column(String(40), nullable=False, default="per_person", server_default="per_person")
    price_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    package: Mapped[Package] = relationship(back_populates="prices")

    def applies_on(self, travel_date: date) -> bool:
        """Check recurring month season and optional inclusive absolute bounds."""
        month = travel_date.month
        if self.start_month <= self.end_month:
            in_season = self.start_month <= month <= self.end_month
        else:
            in_season = month >= self.start_month or month <= self.end_month

        if not in_season:
            return False
        if self.valid_from is not None and travel_date < self.valid_from:
            return False
        if self.valid_until is not None and travel_date > self.valid_until:
            return False
        return True

    def to_snapshot(self) -> dict[str, Any]:
        """Serialize all price fields using JSON-friendly values."""
        return {
            "season_name": self.season_name,
            "start_month": self.start_month,
            "end_month": self.end_month,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "price_per_person": format(self.price_per_person, ".2f"),
            "currency": self.currency,
            "price_basis": self.price_basis,
            "price_note": self.price_note,
        }


class BlogPost(TimestampMixin, Base):
    """Server-rendered travel article with optional related packages."""

    __tablename__ = "blog_posts"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_blog_posts_slug"),
        CheckConstraint("status IN ('draft', 'published')", name="ck_blog_posts_status"),
        CheckConstraint(
            "(status = 'draft' AND published_at IS NULL) OR "
            "(status = 'published' AND published_at IS NOT NULL)",
            name="ck_blog_posts_publication_metadata",
        ),
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="ck_blog_posts_slug_format"),
        Index("ix_blog_posts_status_published", "status", "published_at"),
        Index("ix_blog_posts_category_status", "category", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    slug: Mapped[str] = mapped_column(String(180), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    cover_image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cover_image_alt: Mapped[str | None] = mapped_column(String(200), nullable=True)
    seo_title: Mapped[str | None] = mapped_column(String(160), nullable=True)
    seo_description: Mapped[str | None] = mapped_column(String(320), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", server_default="draft")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    author_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    author: Mapped[User | None] = relationship(back_populates="blog_posts_authored", foreign_keys=[author_id])
    updated_by: Mapped[User | None] = relationship(foreign_keys=[updated_by_id])
    related_packages: Mapped[list[Package]] = relationship(
        secondary=blog_post_packages, order_by="Package.title", passive_deletes=True
    )


class PackageVersion(TimestampMixin, Base):
    """Append-only package snapshot; restoration should append a further row."""

    __tablename__ = "package_versions"
    __table_args__ = (
        CheckConstraint("version_number > 0", name="ck_package_versions_positive_number"),
        CheckConstraint("jsonb_typeof(snapshot) = 'object'", name="ck_package_versions_snapshot_object"),
        UniqueConstraint("package_id", "version_number", name="uq_package_versions_number"),
        Index("ix_package_versions_package_created", "package_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    package_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("packages.id", ondelete="RESTRICT"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    change_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    package: Mapped[Package] = relationship(back_populates="versions")
    created_by: Mapped[User | None] = relationship(back_populates="package_versions")


class UserSession(Base):
    """Opaque server-side staff session and its synchronizer CSRF token."""

    __tablename__ = "user_sessions"
    __table_args__ = (
        Index("ix_user_sessions_user_id", "user_id"),
        Index("ix_user_sessions_expires_at", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User | None] = relationship(back_populates="sessions")


class PackageDraft(TimestampMixin, Base):
    """One mutable pending edit for a published package; not version history."""

    __tablename__ = "package_drafts"
    __table_args__ = (
        CheckConstraint("jsonb_typeof(draft_payload) = 'object'", name="ck_package_drafts_payload_object"),
        UniqueConstraint("package_id", name="uq_package_drafts_package"),
        Index("ix_package_drafts_updated_at", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    package_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("packages.id", ondelete="CASCADE"), nullable=False
    )
    draft_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    base_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    package: Mapped[Package] = relationship(back_populates="pending_draft")
    created_by: Mapped[User | None] = relationship(
        back_populates="package_drafts_created",
        foreign_keys=[created_by_id],
    )
    updated_by: Mapped[User | None] = relationship(
        back_populates="package_drafts_updated",
        foreign_keys=[updated_by_id],
    )


class Enquiry(TimestampMixin, Base):
    """Original customer message and staff handling metadata."""

    __tablename__ = "enquiries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new', 'in_progress', 'waiting_customer', 'closed')",
            name="ck_enquiries_status",
        ),
        Index("ix_enquiries_status_created", "status", "created_at"),
        Index("ix_enquiries_assigned_to", "assigned_to_id"),
        CheckConstraint(
            "(travel_start_date IS NULL AND travel_end_date IS NULL) OR "
            "(travel_start_date IS NOT NULL AND travel_end_date IS NOT NULL)",
            name="ck_enquiries_travel_dates_paired",
        ),
        CheckConstraint(
            "travel_start_date IS NULL OR travel_start_date <= travel_end_date",
            name="ck_enquiries_travel_dates_ordered",
        ),
        CheckConstraint(
            "travel_start_date IS NULL OR travel_dates_flexible IS NOT NULL",
            name="ck_enquiries_travel_flexibility_known",
        ),
        CheckConstraint("adults IS NULL OR adults >= 0", name="ck_enquiries_adults_nonnegative"),
        CheckConstraint("children IS NULL OR children >= 0", name="ck_enquiries_children_nonnegative"),
        CheckConstraint("infants IS NULL OR infants >= 0", name="ck_enquiries_infants_nonnegative"),
        CheckConstraint("budget_amount_inr IS NULL OR budget_amount_inr >= 0", name="ck_enquiries_budget_nonnegative"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    package_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("packages.id", ondelete="SET NULL"), nullable=True
    )
    assigned_to_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    customer_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    customer_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    customer_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    original_message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="new", server_default="new")
    whatsapp_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    whatsapp_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    travel_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    travel_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    travel_dates_flexible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    adults: Mapped[int | None] = mapped_column(Integer, nullable=True)
    children: Mapped[int | None] = mapped_column(Integer, nullable=True)
    infants: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_amount_inr: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    response_notice_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    response_notice_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    package: Mapped[Package | None] = relationship(back_populates="enquiries")
    assigned_to: Mapped[User | None] = relationship(back_populates="assigned_enquiries", foreign_keys=[assigned_to_id])
    notes: Mapped[list[EnquiryNote]] = relationship(
        back_populates="enquiry",
        cascade="all, delete-orphan",
        order_by="EnquiryNote.created_at",
    )
    ai_suggestions: Mapped[list[AISuggestion]] = relationship(
        back_populates="enquiry",
        cascade="all, delete-orphan",
        order_by="AISuggestion.created_at",
    )
    activities: Mapped[list[EnquiryActivity]] = relationship(
        back_populates="enquiry", order_by="EnquiryActivity.created_at"
    )
    replies: Mapped[list[EnquiryReply]] = relationship(
        back_populates="enquiry", order_by="EnquiryReply.created_at"
    )
    quotations: Mapped[list[Quotation]] = relationship(
        back_populates="enquiry", order_by="Quotation.created_at"
    )


class PublicFormNonce(Base):
    """Hashed one-use nonce issued to a public package enquiry form."""

    __tablename__ = "public_form_nonces"
    __table_args__ = (Index("ix_public_form_nonces_expires_at", "expires_at"),)

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    package_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("packages.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EnquiryNote(TimestampMixin, Base):
    """Internal staff note attached to an enquiry."""

    __tablename__ = "enquiry_notes"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    enquiry_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("enquiries.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str] = mapped_column(Text, nullable=False)

    enquiry: Mapped[Enquiry] = relationship(back_populates="notes")
    author: Mapped[User | None] = relationship(back_populates="enquiry_notes")


class EnquiryActivity(Base):
    """Append-only audit record for assignment and status changes."""

    __tablename__ = "enquiry_activities"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('status_changed', 'assignment_changed')",
            name="ck_enquiry_activities_event_type",
        ),
        CheckConstraint(
            "from_status IS NULL OR from_status IN ('new', 'in_progress', 'waiting_customer', 'closed')",
            name="ck_enquiry_activities_from_status",
        ),
        CheckConstraint(
            "to_status IS NULL OR to_status IN ('new', 'in_progress', 'waiting_customer', 'closed')",
            name="ck_enquiry_activities_to_status",
        ),
        CheckConstraint(
            "(event_type = 'status_changed' AND to_status IS NOT NULL) OR "
            "(event_type = 'assignment_changed' AND "
            "from_assigned_to_id IS DISTINCT FROM to_assigned_to_id)",
            name="ck_enquiry_activities_event_payload",
        ),
        Index("ix_enquiry_activities_enquiry_created", "enquiry_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    enquiry_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("enquiries.id", ondelete="RESTRICT"), nullable=False
    )
    actor_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    from_assigned_to_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    to_assigned_to_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    enquiry: Mapped[Enquiry] = relationship(back_populates="activities")
    actor: Mapped[User | None] = relationship(back_populates="enquiry_activities", foreign_keys=[actor_id])
    from_assignee: Mapped[User | None] = relationship(foreign_keys=[from_assigned_to_id])
    to_assignee: Mapped[User | None] = relationship(foreign_keys=[to_assigned_to_id])


class EnquiryReply(TimestampMixin, Base):
    """Customer-facing reply record, separate from private EnquiryNote rows."""

    __tablename__ = "enquiry_replies"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'approved', 'manually_sent')", name="ck_enquiry_replies_status"),
        CheckConstraint(
            "(status = 'draft' AND approved_at IS NULL AND manually_sent_at IS NULL) OR "
            "(status = 'approved' AND approved_at IS NOT NULL AND manually_sent_at IS NULL) OR "
            "(status = 'manually_sent' AND approved_at IS NOT NULL AND manually_sent_at IS NOT NULL)",
            name="ck_enquiry_replies_state_timestamps",
        ),
        Index("ix_enquiry_replies_enquiry_created", "enquiry_id", "created_at"),
        Index("ix_enquiry_replies_status_created", "status", "created_at"),
        Index("ix_enquiry_replies_source_ai_suggestion", "source_ai_suggestion_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    enquiry_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("enquiries.id", ondelete="RESTRICT"), nullable=False
    )
    author_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", server_default="draft")
    approved_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    manually_sent_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    manually_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supersedes_reply_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("enquiry_replies.id", ondelete="SET NULL"), nullable=True
    )
    source_ai_suggestion_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ai_suggestions.id", ondelete="SET NULL"), nullable=True
    )

    enquiry: Mapped[Enquiry] = relationship(back_populates="replies")
    author: Mapped[User | None] = relationship(back_populates="enquiry_replies_authored", foreign_keys=[author_id])
    approved_by: Mapped[User | None] = relationship(
        back_populates="enquiry_replies_approved", foreign_keys=[approved_by_id]
    )
    manually_sent_by: Mapped[User | None] = relationship(
        back_populates="enquiry_replies_marked_sent", foreign_keys=[manually_sent_by_id]
    )
    supersedes: Mapped[EnquiryReply | None] = relationship(remote_side="EnquiryReply.id")
    source_ai_suggestion: Mapped[AISuggestion | None] = relationship(back_populates="originated_replies")


class AISuggestion(TimestampMixin, Base):
    """Persisted AI extraction and suggestions for later staff review."""

    __tablename__ = "ai_suggestions"
    __table_args__ = (
        CheckConstraint(
            "review_status IN ('pending', 'approved', 'rejected')",
            name="ck_ai_suggestions_review_status",
        ),
        CheckConstraint(
            "generation_status IS NULL OR generation_status IN "
            "('pending', 'processing', 'completed', 'failed', 'unavailable')",
            name="ck_ai_suggestions_generation_status",
        ),
        CheckConstraint(
            "extraction_review_status IS NULL OR extraction_review_status IN "
            "('pending', 'accepted', 'corrected')",
            name="ck_ai_suggestions_extraction_review_status",
        ),
        CheckConstraint(
            "failure_code IS NULL OR failure_code IN "
            "('provider_unavailable', 'provider_timeout', 'invalid_provider_response', "
            "'validation_failed', 'generation_failed')",
            name="ck_ai_suggestions_failure_code",
        ),
        CheckConstraint(
            "(extraction_review_status IN ('accepted', 'corrected') AND "
            "extraction_reviewed_by_id IS NOT NULL AND extraction_reviewed_at IS NOT NULL) OR "
            "(extraction_review_status IS NULL OR extraction_review_status = 'pending') AND "
            "extraction_reviewed_by_id IS NULL AND extraction_reviewed_at IS NULL",
            name="ck_ai_suggestions_extraction_review_metadata",
        ),
        Index("ix_ai_suggestions_enquiry_created", "enquiry_id", "created_at"),
        Index("ix_ai_suggestions_generation_status", "generation_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    enquiry_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("enquiries.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    extracted_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    package_suggestions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    draft_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    reviewed_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NULL means a legacy row predating the explicit lifecycle; it does not
    # imply that an AI result was generated or reviewed.
    generation_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default="pending", server_default="pending"
    )
    generation_attempt_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    accepted_extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    extraction_review_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default="pending", server_default="pending"
    )
    extraction_reviewed_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    extraction_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(60), nullable=True)

    enquiry: Mapped[Enquiry] = relationship(back_populates="ai_suggestions")
    reviewed_by: Mapped[User | None] = relationship(
        back_populates="reviewed_ai_suggestions", foreign_keys=[reviewed_by_id]
    )
    extraction_reviewed_by: Mapped[User | None] = relationship(foreign_keys=[extraction_reviewed_by_id])
    extraction_review_events: Mapped[list[AIExtractionReviewEvent]] = relationship(
        back_populates="suggestion", order_by="AIExtractionReviewEvent.created_at"
    )
    originated_replies: Mapped[list[EnquiryReply]] = relationship(back_populates="source_ai_suggestion")


class AIExtractionReviewEvent(Base):
    """Append-only attribution for each human acceptance or correction."""

    __tablename__ = "ai_extraction_review_events"
    __table_args__ = (
        CheckConstraint("action_type IN ('accepted', 'corrected')", name="ck_ai_extraction_review_events_action"),
        Index("ix_ai_extraction_review_events_suggestion_created", "ai_suggestion_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    ai_suggestion_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ai_suggestions.id", ondelete="RESTRICT"), nullable=False
    )
    actor_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    previous_extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    accepted_extraction: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    action_type: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    suggestion: Mapped[AISuggestion] = relationship(back_populates="extraction_review_events")
    actor: Mapped[User | None] = relationship(back_populates="ai_extraction_reviews")


@event.listens_for(PackageVersion, "before_update")
def prevent_package_version_update(mapper: Any, connection: Any, target: PackageVersion) -> None:
    """Package history rows are append-only; restores must append a new row."""
    raise ValueError("Package versions are immutable; append a new version instead.")


@event.listens_for(PackageVersion, "before_delete")
def prevent_package_version_delete(mapper: Any, connection: Any, target: PackageVersion) -> None:
    """Keep historical snapshots available for audit and restore operations."""
    raise ValueError("Package versions cannot be deleted.")


@event.listens_for(EnquiryActivity, "before_update")
def prevent_enquiry_activity_update(mapper: Any, connection: Any, target: EnquiryActivity) -> None:
    """Activity rows are audit evidence and must be corrected by appending another row."""
    raise ValueError("Enquiry activity records are immutable; append a correcting event instead.")


@event.listens_for(EnquiryActivity, "before_delete")
def prevent_enquiry_activity_delete(mapper: Any, connection: Any, target: EnquiryActivity) -> None:
    """Keep enquiry activity history available for review."""
    raise ValueError("Enquiry activity records cannot be deleted.")


@event.listens_for(AIExtractionReviewEvent, "before_update")
def prevent_ai_extraction_review_update(mapper: Any, connection: Any, target: AIExtractionReviewEvent) -> None:
    """Corrections are recorded as new events rather than editing review history."""
    raise ValueError("AI extraction review events are immutable; append a new event instead.")


@event.listens_for(AIExtractionReviewEvent, "before_delete")
def prevent_ai_extraction_review_delete(mapper: Any, connection: Any, target: AIExtractionReviewEvent) -> None:
    """Preserve the audit history of extraction review decisions."""
    raise ValueError("AI extraction review events cannot be deleted.")


@event.listens_for(EnquiryReply, "before_update")
def prevent_approved_reply_body_update(mapper: Any, connection: Any, target: EnquiryReply) -> None:
    """Approved text is immutable; corrections must be recorded as a new draft."""
    state = inspect(target)
    if not state.attrs.body.history.has_changes():
        return
    prior_status = state.attrs.status.history.deleted
    if target.status != "draft" or any(status in {"approved", "manually_sent"} for status in prior_status):
        raise ValueError("Approved replies cannot be edited; create a new draft reply instead.")


class Quotation(TimestampMixin, Base):
    """Staff-prepared quotation snapshot attached to an enquiry."""

    __tablename__ = "quotations"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'approved')", name="ck_quotations_status"),
        CheckConstraint(
            "(status = 'draft' AND approved_at IS NULL) OR "
            "(status = 'approved' AND approved_at IS NOT NULL)",
            name="ck_quotations_approval_metadata",
        ),
        CheckConstraint("adults >= 1 AND adults <= 100", name="ck_quotations_adults_range"),
        CheckConstraint("children >= 0 AND children <= 100", name="ck_quotations_children_range"),
        CheckConstraint("infants >= 0 AND infants <= 100", name="ck_quotations_infants_range"),
        CheckConstraint("adult_unit_price >= 0", name="ck_quotations_adult_price_nonnegative"),
        CheckConstraint("child_unit_price IS NULL OR child_unit_price >= 0", name="ck_quotations_child_price_nonnegative"),
        CheckConstraint("infant_unit_price IS NULL OR infant_unit_price >= 0", name="ck_quotations_infant_price_nonnegative"),
        CheckConstraint("gst_rate_percent IS NULL OR gst_rate_percent BETWEEN 0 AND 100", name="ck_quotations_gst_range"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_quotations_currency_code"),
        CheckConstraint("price_source IN ('package_price', 'manual')", name="ck_quotations_price_source"),
        CheckConstraint(
            "total_amount IS NULL OR (subtotal_amount IS NOT NULL AND "
            "((gst_rate_percent IS NULL AND gst_amount IS NULL) OR "
            "(gst_rate_percent IS NOT NULL AND gst_amount IS NOT NULL)))",
            name="ck_quotations_total_components",
        ),
        CheckConstraint(
            "(travel_start_date IS NULL AND travel_end_date IS NULL) OR "
            "(travel_start_date IS NOT NULL AND travel_end_date IS NOT NULL AND travel_start_date <= travel_end_date)",
            name="ck_quotations_travel_dates",
        ),
        Index("ix_quotations_enquiry_created", "enquiry_id", "created_at"),
        Index("uq_quotations_share_token_hash", "share_token_hash", unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    enquiry_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("enquiries.id", ondelete="RESTRICT"), nullable=False
    )
    package_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("packages.id", ondelete="SET NULL"), nullable=True
    )
    package_price_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("package_prices.id", ondelete="SET NULL"), nullable=True
    )
    created_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", server_default="draft")
    package_code_snapshot: Mapped[str | None] = mapped_column(String(32), nullable=True)
    package_title_snapshot: Mapped[str] = mapped_column(String(240), nullable=False)
    customer_name_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    travel_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    travel_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    adults: Mapped[int] = mapped_column(Integer, nullable=False)
    children: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    infants: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accommodation: Mapped[str] = mapped_column(String(160), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR", server_default="INR")
    price_source: Mapped[str] = mapped_column(String(20), nullable=False)
    price_basis: Mapped[str] = mapped_column(String(80), nullable=False, default="per person, twin sharing")
    adult_unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    child_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    infant_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    staff_price_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    gst_rate_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    subtotal_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    gst_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    inclusions: Mapped[str | None] = mapped_column(Text, nullable=True)
    exclusions: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    share_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    share_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    enquiry: Mapped[Enquiry] = relationship(back_populates="quotations")
    package: Mapped[Package | None] = relationship()
    package_price: Mapped[PackagePrice | None] = relationship()
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_id])
