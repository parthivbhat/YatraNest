from io import BytesIO
import struct
import zlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import BlogPost, User, UserSession
from app.services.blog_cms import remove_new_cover
from app.services.session_store import SESSION_COOKIE_NAME, issue_session


def _user(db: Session, role: str) -> User:
    user = User(name=f"Blog {role}", email=f"{uuid4()}@example.test", role=role)
    db.add(user)
    db.flush()
    return user


def _png_1px() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x20\x60\x30\xff"))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def blog_client_factory(db_session: Session):
    app.dependency_overrides[get_db] = lambda: db_session

    def make_client(user: User | None):
        client = TestClient(app, follow_redirects=False)
        csrf = None
        if user is not None:
            _, token = issue_session(db_session, user_id=user.id)
            db_session.flush()
            csrf = db_session.query(UserSession).filter_by(user_id=user.id).one().csrf_token
            client.cookies.set(SESSION_COOKIE_NAME, token)
        return client, csrf

    try:
        yield make_client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _article_data(category: str = "Destination guide") -> dict[str, str]:
    return {
        "title": "A thoughtful guide to Munnar",
        "slug": "munnar-guide",
        "content": "Munnar has green hills.\n\nPlan at your own pace.",
        "category": category,
        "cover_image_alt": "Tea gardens across Munnar hills",
        "seo_title": "Munnar travel guide | YatraNest",
        "seo_description": "Ideas for planning a trip to Munnar.",
    }


def test_editor_can_create_draft_preview_publish_and_unpublish_with_related_package(
    db_session: Session, supplied_packages, blog_client_factory
):
    editor = _user(db_session, "editor")
    package = supplied_packages["kerala"]
    client, csrf = blog_client_factory(editor)
    image_path = None
    try:
        form = client.get("/admin/blog/new")
        assert form.status_code == 200
        assert 'enctype="multipart/form-data"' in form.text

        response = client.post(
            "/admin/blog",
            data={**_article_data(), "csrf_token": csrf, "related_package_ids": str(package.id)},
            files={"cover_image": ("cover.png", _png_1px(), "image/png")},
        )
        assert response.status_code == 303
        assert response.headers["location"].endswith("/preview")
        post = db_session.scalar(select(BlogPost).where(BlogPost.slug == "munnar-guide"))
        assert post is not None
        assert post.status == "draft" and post.published_at is None
        assert post.author_id == editor.id
        assert post.related_packages == [package]
        assert post.cover_image_path and post.cover_image_path.endswith(".png")
        image_path = post.cover_image_path

        preview = client.get(response.headers["location"])
        assert preview.status_code == 200
        assert preview.headers["cache-control"] == "no-store"
        assert "Staff preview" in preview.text
        assert "Munnar has green hills." in preview.text
        assert "Publish post" in preview.text
        assert "Unpublish" not in preview.text

        public_list = client.get("/blog")
        assert public_list.status_code == 200
        assert "A thoughtful guide to Munnar" not in public_list.text
        assert client.get("/blog/munnar-guide").status_code == 404

        published = client.post(f"/admin/blog/{post.id}/publish", data={"csrf_token": csrf})
        assert published.status_code == 303
        assert published.headers["location"] == f"/admin/blog/{post.id}/preview"
        assert post.status == "published" and post.published_at is not None
        published_preview = client.get(published.headers["location"])
        assert "Published · visible publicly" in published_preview.text
        assert "Staff preview · not publicly published" not in published_preview.text
        assert "Unpublish" in published_preview.text
        assert "Publish post" not in published_preview.text

        listing = client.get("/blog?category=Destination%20guide")
        assert listing.status_code == 200
        assert "A thoughtful guide to Munnar" in listing.text
        assert post.cover_image_path in listing.text
        detail = client.get("/blog/munnar-guide")
        assert detail.status_code == 200
        assert "Munnar travel guide | YatraNest" in detail.text
        assert "View journey &amp; enquire" in detail.text
        assert "/packages/munnar-thekkady-alleppey-houseboat" in detail.text
        assert "<script>" not in detail.text

        unpublished = client.post(f"/admin/blog/{post.id}/unpublish", data={"csrf_token": csrf})
        assert unpublished.status_code == 303
        assert unpublished.headers["location"] == "/admin/blog"
        assert post.status == "draft" and post.published_at is None
        draft_preview = client.get(f"/admin/blog/{post.id}/preview")
        assert "Staff preview · not publicly published" in draft_preview.text
        assert "Publish post" in draft_preview.text
        assert "Unpublish" not in draft_preview.text
        assert client.get("/blog/munnar-guide").status_code == 404
    finally:
        client.close()
        remove_new_cover(image_path)


def test_admin_can_create_and_editor_can_save_edits_as_draft(
    db_session: Session, supplied_packages, blog_client_factory
):
    admin = _user(db_session, "admin")
    client, csrf = blog_client_factory(admin)
    try:
        created = client.post("/admin/blog", data={**_article_data("Planning"), "csrf_token": csrf})
        assert created.status_code == 303
        post = db_session.scalar(select(BlogPost).where(BlogPost.slug == "munnar-guide"))
        assert post is not None and post.author_id == admin.id

        editor = _user(db_session, "editor")
        editor_client, editor_csrf = blog_client_factory(editor)
        try:
            edited = editor_client.post(
                f"/admin/blog/{post.id}/edit",
                data={**_article_data("Updated guide"), "title": "Updated Munnar guide", "csrf_token": editor_csrf},
            )
            assert edited.status_code == 303
            assert post.title == "Updated Munnar guide"
            assert post.status == "draft"
            assert post.updated_by_id == editor.id
        finally:
            editor_client.close()
    finally:
        client.close()


def test_consultant_and_unauthenticated_users_cannot_manage_blog(db_session: Session, blog_client_factory):
    consultant = _user(db_session, "consultant")
    consultant_client, _ = blog_client_factory(consultant)
    anonymous_client, _ = blog_client_factory(None)
    try:
        denied = consultant_client.get("/admin/blog")
        assert denied.status_code == 403
        assert consultant_client.get("/admin/blog/new").status_code == 403
        assert 'href="/admin/blog"' not in denied.text
        assert anonymous_client.get("/admin/blog").status_code == 303
        assert consultant_client.post("/admin/blog", data={}).status_code == 403
    finally:
        consultant_client.close()
        anonymous_client.close()


def test_blog_mutations_require_csrf_and_upload_rejects_invalid_image(db_session: Session, blog_client_factory):
    editor = _user(db_session, "editor")
    client, csrf = blog_client_factory(editor)
    try:
        missing_csrf = client.post("/admin/blog", data=_article_data())
        assert missing_csrf.status_code == 403

        invalid_image = client.post(
            "/admin/blog",
            data={**_article_data(), "csrf_token": csrf},
            files={"cover_image": ("not-really.png", b"<script>no</script>", "image/png")},
        )
        assert invalid_image.status_code == 422
        assert "valid JPEG or PNG" in invalid_image.text
        assert db_session.scalar(select(BlogPost.id)) is None
    finally:
        client.close()


def test_only_published_related_packages_are_accepted(db_session: Session, supplied_packages, blog_client_factory):
    editor = _user(db_session, "editor")
    client, csrf = blog_client_factory(editor)
    try:
        rejected = client.post(
            "/admin/blog",
            data={**_article_data(), "csrf_token": csrf, "related_package_ids": str(supplied_packages["south_india"].id)},
        )
        assert rejected.status_code == 422
        assert "currently published" in rejected.text
        assert db_session.scalar(select(BlogPost.id)) is None
    finally:
        client.close()
