"""Validation, package association and cover-upload handling for the blog CMS."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import struct
import unicodedata
import uuid
import zlib

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import BlogPost, Package, User


MAX_COVER_BYTES = 5 * 1024 * 1024
BLOG_UPLOAD_DIR = Path(__file__).resolve().parents[1] / "static" / "uploads" / "blog"
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class BlogValidationError(ValueError):
    pass


def latest_published_blog_posts(session: Session, *, limit: int = 3) -> list[BlogPost]:
    """Return at most three public posts, newest publication first."""
    bounded_limit = max(0, min(limit, 3))
    if bounded_limit == 0:
        return []
    return list(session.scalars(
        select(BlogPost)
        .where(BlogPost.status == "published")
        .order_by(BlogPost.published_at.desc(), BlogPost.id.desc())
        .limit(bounded_limit)
    ))


def make_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")


def _png_extension(data: bytes) -> str | None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 45:
        return None
    offset = 8
    saw_header = False
    saw_data = False
    saw_end = False
    width = height = 0
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + size
        if size > MAX_COVER_BYTES or end > len(data):
            return None
        chunk = data[offset + 4:offset + 8 + size]
        expected_crc = int.from_bytes(data[offset + 8 + size:end], "big")
        if zlib.crc32(chunk) & 0xFFFFFFFF != expected_crc:
            return None
        if not saw_header:
            if kind != b"IHDR" or size != 13:
                return None
            width, height = struct.unpack(">II", data[offset + 8:offset + 16])
            saw_header = True
        elif kind == b"IDAT":
            saw_data = True
        elif kind == b"IEND":
            if size != 0 or end != len(data):
                return None
            saw_end = True
            break
        offset = end
    if not (saw_header and saw_data and saw_end and 0 < width <= 10000 and 0 < height <= 10000 and width * height <= 20_000_000):
        return None
    return ".png"


def _jpeg_extension(data: bytes) -> str | None:
    if len(data) < 20 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        return None
    offset = 2
    dimensions: tuple[int, int] | None = None
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while offset + 4 <= len(data) - 2:
        if data[offset] != 0xFF:
            return None
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            return None
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue
        if offset + 2 > len(data):
            return None
        segment_size = int.from_bytes(data[offset:offset + 2], "big")
        if segment_size < 2 or offset + segment_size > len(data):
            return None
        if marker in sof_markers:
            if segment_size < 7:
                return None
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            dimensions = (width, height)
        if marker == 0xDA:  # Start of scan; dimensions have already been read from SOF.
            break
        offset += segment_size
    if dimensions is None:
        return None
    width, height = dimensions
    return ".jpg" if 0 < width <= 10000 and 0 < height <= 10000 and width * height <= 20_000_000 else None


def validate_cover_image(data: bytes, content_type: str | None) -> str:
    """Accept only bounded, structurally valid JPEG/PNG image bytes."""
    if not data or len(data) > MAX_COVER_BYTES:
        raise BlogValidationError("Cover image must be no larger than 5 MB.")
    extension = _png_extension(data) if data.startswith(b"\x89PNG") else _jpeg_extension(data)
    expected_type = {".png": "image/png", ".jpg": "image/jpeg"}.get(extension)
    if extension is None or content_type != expected_type:
        raise BlogValidationError("Upload a valid JPEG or PNG cover image.")
    return extension


def store_cover_image(data: bytes, content_type: str | None) -> str:
    extension = validate_cover_image(data, content_type)
    BLOG_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{extension}"
    root = BLOG_UPLOAD_DIR.resolve()
    destination = (root / filename).resolve()
    if destination.parent != root:
        raise BlogValidationError("Cover image could not be stored safely.")
    with destination.open("xb") as image_file:
        image_file.write(data)
    return f"/static/uploads/blog/{filename}"


def remove_new_cover(path: str | None) -> None:
    if not path:
        return
    root = BLOG_UPLOAD_DIR.resolve()
    candidate = (root / Path(path).name).resolve()
    if candidate.parent == root and candidate.exists():
        candidate.unlink()


def _validated_fields(*, title: str, slug: str, content: str, category: str,
                      cover_image_alt: str, seo_title: str, seo_description: str) -> dict[str, str | None]:
    title = title.strip()
    content = content.strip()
    category = category.strip()
    slug = make_slug(slug.strip() or title)
    alt = cover_image_alt.strip()
    seo_title = seo_title.strip()
    seo_description = seo_description.strip()
    if not title or len(title) > 240:
        raise BlogValidationError("Title is required and must be at most 240 characters.")
    if not slug or len(slug) > 180 or not _SLUG_RE.fullmatch(slug):
        raise BlogValidationError("Enter a valid URL slug using lowercase letters, numbers and hyphens.")
    if not content or len(content) > 100_000:
        raise BlogValidationError("Content is required and must be at most 100,000 characters.")
    if not category or len(category) > 100:
        raise BlogValidationError("Category is required and must be at most 100 characters.")
    if len(alt) > 200 or len(seo_title) > 160 or len(seo_description) > 320:
        raise BlogValidationError("Cover alt text or SEO metadata exceeds its length limit.")
    return {
        "title": title,
        "slug": slug,
        "content": content,
        "category": category,
        "cover_image_alt": alt or None,
        "seo_title": seo_title or None,
        "seo_description": seo_description or None,
    }


def _related_packages(session: Session, package_ids: list[uuid.UUID]) -> list[Package]:
    unique_ids = list(dict.fromkeys(package_ids))
    packages = list(session.scalars(
        select(Package).where(Package.id.in_(unique_ids), Package.status == "published").order_by(Package.title)
    )) if unique_ids else []
    if len(packages) != len(unique_ids):
        raise BlogValidationError("Related packages must all be currently published.")
    return packages


def create_blog_post(session: Session, *, actor: User, fields: dict[str, str],
                     package_ids: list[uuid.UUID], cover_image_path: str | None = None) -> BlogPost:
    if not actor.is_active or actor.role not in {"admin", "editor"}:
        raise BlogValidationError("You do not have permission to manage blog posts.")
    field_names = ("title", "slug", "content", "category", "cover_image_alt", "seo_title", "seo_description")
    values = _validated_fields(**{name: str(fields.get(name, "")) for name in field_names})
    if cover_image_path and not values["cover_image_alt"]:
        raise BlogValidationError("Add descriptive alt text for the cover image.")
    duplicate = session.scalar(select(BlogPost.id).where(BlogPost.slug == values["slug"]))
    if duplicate:
        raise BlogValidationError("That slug is already in use.")
    post = BlogPost(**values, cover_image_path=cover_image_path, status="draft", author_id=actor.id,
                    updated_by_id=actor.id, related_packages=_related_packages(session, package_ids))
    session.add(post)
    session.flush()
    return post


def update_blog_post(session: Session, *, post_id: uuid.UUID, actor: User, fields: dict[str, str],
                     package_ids: list[uuid.UUID], cover_image_path: str | None = None) -> BlogPost:
    if not actor.is_active or actor.role not in {"admin", "editor"}:
        raise BlogValidationError("You do not have permission to manage blog posts.")
    post = session.scalar(select(BlogPost).where(BlogPost.id == post_id).with_for_update())
    if post is None:
        raise BlogValidationError("Blog post not found.")
    field_names = ("title", "slug", "content", "category", "cover_image_alt", "seo_title", "seo_description")
    values = _validated_fields(**{name: str(fields.get(name, "")) for name in field_names})
    duplicate = session.scalar(select(BlogPost.id).where(BlogPost.slug == values["slug"], BlogPost.id != post.id))
    if duplicate:
        raise BlogValidationError("That slug is already in use.")
    if (cover_image_path or post.cover_image_path) and not values["cover_image_alt"]:
        raise BlogValidationError("Add descriptive alt text for the cover image.")
    for key, value in values.items():
        setattr(post, key, value)
    post.related_packages = _related_packages(session, package_ids)
    if cover_image_path:
        post.cover_image_path = cover_image_path
    # Every edit is saved as a draft and must be reviewed/published again.
    post.status = "draft"
    post.published_at = None
    post.updated_by_id = actor.id
    session.flush()
    return post


def get_blog_post_for_edit(session: Session, post_id: uuid.UUID) -> BlogPost | None:
    return session.scalar(select(BlogPost).where(BlogPost.id == post_id).options(
        selectinload(BlogPost.related_packages)
    ))


def change_blog_publication(session: Session, *, post_id: uuid.UUID, actor: User,
                            publish: bool) -> BlogPost:
    if not actor.is_active or actor.role not in {"admin", "editor"}:
        raise BlogValidationError("You do not have permission to manage blog posts.")
    post = session.scalar(select(BlogPost).where(BlogPost.id == post_id).with_for_update())
    if post is None:
        raise BlogValidationError("Blog post not found.")
    post.status = "published" if publish else "draft"
    post.published_at = datetime.now(timezone.utc) if publish else None
    post.updated_by_id = actor.id
    session.flush()
    return post
