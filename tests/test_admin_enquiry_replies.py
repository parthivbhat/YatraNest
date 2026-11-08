"""PostgreSQL-backed tests for the staff reply lifecycle."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Enquiry, EnquiryNote, EnquiryReply, Package, User
from app.services.session_store import SESSION_COOKIE_NAME, find_session, issue_session


@pytest.fixture
def reply_client_factory(db_session: Session):
    app.dependency_overrides[get_db] = lambda: db_session

    def make_client(user: User | None, *, raises: bool = True) -> TestClient:
        client = TestClient(app, follow_redirects=False, raise_server_exceptions=raises)
        if user is not None:
            _, token = issue_session(db_session, user_id=user.id)
            db_session.flush()
            client.cookies.set(SESSION_COOKIE_NAME, token)
        return client

    try:
        yield make_client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _user(db: Session, role: str) -> User:
    user = User(name=f"{role.title()} {uuid4().hex[:7]}", email=f"{uuid4()}@example.test", role=role)
    db.add(user)
    db.flush()
    return user


def _enquiry(db: Session, assigned: User | None = None) -> Enquiry:
    package = Package(
        package_code=f"RPL-{uuid4().hex[:8]}", slug=f"reply-test-{uuid4().hex[:8]}",
        title="Reply workflow test package", status="published", duration_days=4,
    )
    enquiry = Enquiry(
        package=package, assigned_to=assigned, customer_name="Reply QA traveller",
        customer_email="reply-qa@example.invalid", original_message="SYNTHETIC REPLY QA REQUEST",
        status="new",
    )
    db.add(enquiry)
    db.flush()
    return enquiry


def _workspace_tokens(client: TestClient, enquiry: Enquiry) -> tuple[str, str]:
    import re

    response = client.get(f"/admin/enquiries/{enquiry.id}")
    assert response.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    expected = re.search(r'name="expected_updated_at" value="([^"]+)"', response.text)
    assert csrf and expected
    return csrf.group(1), expected.group(1)


def _draft(db: Session, enquiry: Enquiry, author: User, body: str = "Reply draft") -> EnquiryReply:
    reply = EnquiryReply(enquiry_id=enquiry.id, author_id=author.id, body=body, status="draft")
    db.add(reply)
    db.flush()
    return reply


def _approve(reply: EnquiryReply, admin: User) -> None:
    approver_id = admin.id
    reply.status = "approved"
    reply.approved_by_id = approver_id
    reply.approved_at = datetime.now(UTC)


def test_admin_editor_and_assigned_consultant_can_create_authored_drafts(db_session, reply_client_factory):
    admin = _user(db_session, "admin")
    editor = _user(db_session, "editor")
    consultant = _user(db_session, "consultant")
    for actor, assignee in ((admin, None), (editor, None), (consultant, consultant)):
        enquiry = _enquiry(db_session, assignee)
        client = reply_client_factory(actor)
        csrf, expected = _workspace_tokens(client, enquiry)
        response = client.post(f"/admin/enquiries/{enquiry.id}/replies", data={
            "csrf_token": csrf, "expected_enquiry_updated_at": expected, "body": "  Hello from staff.  ",
        })
        assert response.status_code == 303
        reply = db_session.scalar(select(EnquiryReply).where(EnquiryReply.enquiry_id == enquiry.id))
        assert (reply.author_id, reply.body, reply.status) == (actor.id, "Hello from staff.", "draft")
        assert reply.approved_by_id is None and reply.approved_at is None
        assert reply.manually_sent_by_id is None and reply.manually_sent_at is None
        client.close()


@pytest.mark.parametrize("assignment", ["unassigned", "another"])
def test_consultant_cannot_draft_for_unassigned_or_other_enquiry(db_session, reply_client_factory, assignment):
    consultant, other = _user(db_session, "consultant"), _user(db_session, "consultant")
    enquiry = _enquiry(db_session, None if assignment == "unassigned" else other)
    client = reply_client_factory(consultant)
    session = find_session(db_session, client.cookies.get(SESSION_COOKIE_NAME))
    response = client.post(f"/admin/enquiries/{enquiry.id}/replies", data={
        "csrf_token": session.csrf_token,
        "expected_enquiry_updated_at": enquiry.updated_at.isoformat(),
        "body": "No access",
    })
    assert response.status_code == 404
    assert db_session.scalar(select(func.count(EnquiryReply.id)).where(EnquiryReply.enquiry_id == enquiry.id)) == 0


@pytest.mark.parametrize("body", ["  \n ", "x" * 10_001])
def test_empty_or_oversized_reply_rejected(db_session, reply_client_factory, body):
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session)
    client = reply_client_factory(admin)
    csrf, expected = _workspace_tokens(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/replies", data={
        "csrf_token": csrf, "expected_enquiry_updated_at": expected, "body": body,
    })
    assert response.status_code == 422
    assert db_session.scalar(select(func.count(EnquiryReply.id)).where(EnquiryReply.enquiry_id == enquiry.id)) == 0


def test_draft_edit_works_but_approved_reply_cannot_be_edited(db_session, reply_client_factory):
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session)
    reply = _draft(db_session, enquiry, admin)
    client = reply_client_factory(admin)
    csrf, enquiry_updated = _workspace_tokens(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/edit", data={
        "csrf_token": csrf, "expected_enquiry_updated_at": enquiry_updated,
        "expected_reply_updated_at": reply.updated_at.isoformat(), "body": "Corrected draft",
    })
    assert response.status_code == 303
    db_session.refresh(reply)
    assert reply.body == "Corrected draft"
    _approve(reply, admin)
    db_session.flush()
    csrf, enquiry_updated = _workspace_tokens(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/edit", data={
        "csrf_token": csrf, "expected_enquiry_updated_at": enquiry_updated,
        "expected_reply_updated_at": reply.updated_at.isoformat(), "body": "Attempted change",
    })
    assert response.status_code == 409
    db_session.refresh(reply)
    assert reply.body == "Corrected draft"


def test_admin_approval_records_actor_and_is_distinct_from_sending(db_session, reply_client_factory):
    admin = _user(db_session, "admin")
    author = _user(db_session, "editor")
    enquiry = _enquiry(db_session)
    reply = _draft(db_session, enquiry, author)
    client = reply_client_factory(admin)
    csrf, enquiry_updated = _workspace_tokens(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/approve", data={
        "csrf_token": csrf, "expected_enquiry_updated_at": enquiry_updated,
        "expected_reply_updated_at": reply.updated_at.isoformat(),
    })
    assert response.status_code == 303
    db_session.refresh(reply)
    assert reply.status == "approved" and reply.approved_by_id == admin.id and reply.approved_at is not None
    assert reply.manually_sent_by_id is None and reply.manually_sent_at is None


@pytest.mark.parametrize("role", ["editor", "consultant"])
def test_only_admin_can_approve(db_session, reply_client_factory, role):
    admin, actor = _user(db_session, "admin"), _user(db_session, role)
    enquiry = _enquiry(db_session, actor if role == "consultant" else None)
    reply = _draft(db_session, enquiry, actor)
    client = reply_client_factory(actor)
    csrf, enquiry_updated = _workspace_tokens(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/approve", data={
        "csrf_token": csrf, "expected_enquiry_updated_at": enquiry_updated,
        "expected_reply_updated_at": reply.updated_at.isoformat(),
    })
    assert response.status_code == 403
    db_session.refresh(reply)
    assert reply.status == "draft" and reply.approved_by_id is None


def test_duplicate_approval_and_approval_of_sent_reply_are_rejected(db_session, reply_client_factory):
    admin, sender = _user(db_session, "admin"), _user(db_session, "editor")
    enquiry = _enquiry(db_session)
    approved = _draft(db_session, enquiry, sender)
    _approve(approved, admin)
    sent = _draft(db_session, enquiry, sender, "Already sent")
    _approve(sent, admin)
    sent.status = "manually_sent"
    sent.manually_sent_by_id = sender.id
    sent.manually_sent_at = datetime.now(UTC)
    db_session.flush()
    client = reply_client_factory(admin)
    csrf, enquiry_updated = _workspace_tokens(client, enquiry)
    for reply in (approved, sent):
        response = client.post(f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/approve", data={
            "csrf_token": csrf, "expected_enquiry_updated_at": enquiry_updated,
            "expected_reply_updated_at": reply.updated_at.isoformat(),
        })
        assert response.status_code == 409


def test_approved_body_is_immutable_and_correction_can_be_a_new_draft(db_session, reply_client_factory):
    admin, author = _user(db_session, "admin"), _user(db_session, "editor")
    enquiry = _enquiry(db_session)
    approved = _draft(db_session, enquiry, author, "Approved original")
    _approve(approved, admin)
    db_session.flush()
    client = reply_client_factory(admin)
    csrf, expected = _workspace_tokens(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/replies", data={
        "csrf_token": csrf, "expected_enquiry_updated_at": expected,
        "supersedes_reply_id": str(approved.id), "body": "Separate correction draft",
    })
    assert response.status_code == 303
    db_session.refresh(approved)
    replies = list(db_session.scalars(select(EnquiryReply).where(EnquiryReply.enquiry_id == enquiry.id)))
    assert len(replies) == 2
    assert approved.body == "Approved original"
    correction = next(reply for reply in replies if reply.body == "Separate correction draft")
    assert correction.status == "draft" and correction.supersedes_reply_id == approved.id
    approved.body = "Forbidden mutation"
    with pytest.raises(ValueError, match="Approved replies cannot be edited"):
        db_session.flush()
    db_session.rollback()


def test_admin_editor_and_authored_consultant_can_record_manual_send(db_session, reply_client_factory):
    admin, editor, consultant = _user(db_session, "admin"), _user(db_session, "editor"), _user(db_session, "consultant")
    cases = ((admin, None, editor), (editor, None, editor), (consultant, consultant, consultant))
    for actor, assignee, author in cases:
        enquiry = _enquiry(db_session, assignee)
        reply = _draft(db_session, enquiry, author, "Approved and manually shared content")
        _approve(reply, admin)
        db_session.flush()
        client = reply_client_factory(actor)
        confirmation = client.get(f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/manual-sent/confirm")
        assert confirmation.status_code == 200
        assert "This application does not send replies" in confirmation.text
        import re
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', confirmation.text)
        enquiry_updated = re.search(r'name="expected_enquiry_updated_at" value="([^"]+)"', confirmation.text)
        reply_updated = re.search(r'name="expected_reply_updated_at" value="([^"]+)"', confirmation.text)
        assert csrf and enquiry_updated and reply_updated
        assert 'name="confirm_already_sent"' in confirmation.text
        response = client.post(f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/manual-sent", data={
            "csrf_token": csrf.group(1), "expected_enquiry_updated_at": enquiry_updated.group(1),
            "expected_reply_updated_at": reply_updated.group(1), "confirm_already_sent": "yes",
        })
        assert response.status_code == 303
        db_session.refresh(reply)
        assert reply.status == "manually_sent" and reply.manually_sent_by_id == actor.id
        assert reply.manually_sent_at is not None and reply.body == "Approved and manually shared content"
        client.close()


def test_consultant_cannot_mark_other_authors_reply_as_sent(db_session, reply_client_factory):
    admin, consultant, other_author = _user(db_session, "admin"), _user(db_session, "consultant"), _user(db_session, "editor")
    enquiry = _enquiry(db_session, consultant)
    reply = _draft(db_session, enquiry, other_author)
    _approve(reply, admin)
    db_session.flush()
    client = reply_client_factory(consultant)
    response = client.get(f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/manual-sent/confirm")
    assert response.status_code == 404
    response = client.post(f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/manual-sent", data={
        "csrf_token": _workspace_tokens(client, enquiry)[0], "expected_enquiry_updated_at": _workspace_tokens(client, enquiry)[1],
        "expected_reply_updated_at": reply.updated_at.isoformat(), "confirm_already_sent": "yes",
    })
    assert response.status_code == 403
    db_session.refresh(reply)
    assert reply.status == "approved" and reply.manually_sent_by_id is None


def test_draft_and_repeated_manual_sent_are_rejected(db_session, reply_client_factory):
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session)
    reply = _draft(db_session, enquiry, admin)
    client = reply_client_factory(admin)
    csrf, enquiry_updated = _workspace_tokens(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/manual-sent", data={
        "csrf_token": csrf, "expected_enquiry_updated_at": enquiry_updated,
        "expected_reply_updated_at": reply.updated_at.isoformat(), "confirm_already_sent": "yes",
    })
    assert response.status_code == 409
    _approve(reply, admin)
    db_session.flush()
    confirmation = client.get(f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/manual-sent/confirm")
    import re
    csrf_value = re.search(r'name="csrf_token" value="([^"]+)"', confirmation.text).group(1)
    enquiry_value = re.search(r'name="expected_enquiry_updated_at" value="([^"]+)"', confirmation.text).group(1)
    reply_value = re.search(r'name="expected_reply_updated_at" value="([^"]+)"', confirmation.text).group(1)
    assert client.post(f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/manual-sent", data={
        "csrf_token": csrf_value, "expected_enquiry_updated_at": enquiry_value,
        "expected_reply_updated_at": reply_value, "confirm_already_sent": "yes",
    }).status_code == 303
    sent_by = reply.manually_sent_by_id
    sent_at = reply.manually_sent_at
    stale = client.post(f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/manual-sent", data={
        "csrf_token": csrf_value, "expected_enquiry_updated_at": enquiry_value,
        "expected_reply_updated_at": reply_value, "confirm_already_sent": "yes",
    })
    assert stale.status_code == 409
    db_session.refresh(reply)
    assert reply.manually_sent_by_id == sent_by and reply.manually_sent_at == sent_at


@pytest.mark.parametrize("action", ["create", "edit", "approve", "manual_sent"])
def test_every_reply_post_requires_csrf(db_session, reply_client_factory, action):
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session)
    reply = _draft(db_session, enquiry, admin)
    client = reply_client_factory(admin)
    _, enquiry_updated = _workspace_tokens(client, enquiry)
    base = {"csrf_token": "", "expected_enquiry_updated_at": enquiry_updated,
            "expected_reply_updated_at": reply.updated_at.isoformat(), "body": "A draft", "confirm_already_sent": "yes"}
    paths = {
        "create": (f"/admin/enquiries/{enquiry.id}/replies", base),
        "edit": (f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/edit", base),
        "approve": (f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/approve", base),
        "manual_sent": (f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/manual-sent", base),
    }
    path, data = paths[action]
    assert client.post(path, data=data).status_code == 403


def test_approval_commit_failure_rolls_back_state_and_attribution(db_session, reply_client_factory, monkeypatch):
    from app.routes import admin as admin_routes

    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session)
    reply = _draft(db_session, enquiry, admin)
    client = reply_client_factory(admin, raises=False)
    csrf, enquiry_updated = _workspace_tokens(client, enquiry)
    original_approve = admin_routes.approve_reply

    def approve_then_fail(*args, **kwargs):
        original_approve(*args, **kwargs)
        raise RuntimeError("Injected failure after reply state flush")

    monkeypatch.setattr(admin_routes, "approve_reply", approve_then_fail)
    response = client.post(f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/approve", data={
        "csrf_token": csrf, "expected_enquiry_updated_at": enquiry_updated,
        "expected_reply_updated_at": reply.updated_at.isoformat(),
    })
    assert response.status_code == 500
    db_session.refresh(reply)
    assert reply.status == "draft" and reply.approved_by_id is None and reply.approved_at is None


def test_unauthenticated_reply_posts_redirect_to_login(db_session, reply_client_factory):
    enquiry = _enquiry(db_session)
    response = reply_client_factory(None).post(f"/admin/enquiries/{enquiry.id}/replies", data={})
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_reply_text_is_private_and_notes_remain_separate(db_session, reply_client_factory):
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session)
    reply = _draft(db_session, enquiry, admin, "PRIVATE_REPLY_BODY_FOR_QA")
    note = EnquiryNote(enquiry_id=enquiry.id, author_id=admin.id, note="PRIVATE_INTERNAL_NOTE_FOR_QA")
    db_session.add(note)
    db_session.flush()
    client = reply_client_factory(admin)
    workspace = client.get(f"/admin/enquiries/{enquiry.id}")
    assert workspace.status_code == 200
    assert "PRIVATE_REPLY_BODY_FOR_QA" in workspace.text
    assert "PRIVATE_INTERNAL_NOTE_FOR_QA" in workspace.text
    assert "Internal notes" in workspace.text and "Replies" in workspace.text
    public = TestClient(app).get(f"/packages/{enquiry.package.slug}")
    assert public.status_code == 200
    assert "PRIVATE_REPLY_BODY_FOR_QA" not in public.text
    assert "PRIVATE_INTERNAL_NOTE_FOR_QA" not in public.text


def test_private_confirmation_has_no_store_and_noindex(db_session, reply_client_factory):
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session)
    reply = _draft(db_session, enquiry, admin)
    _approve(reply, admin)
    db_session.flush()
    response = reply_client_factory(admin).get(
        f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/manual-sent/confirm"
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
