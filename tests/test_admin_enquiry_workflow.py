"""PostgreSQL-backed tests for staff enquiry workflow actions."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Enquiry, EnquiryActivity, EnquiryNote, Package, PackagePrice, User
from app.services.session_store import SESSION_COOKIE_NAME, issue_session


@pytest.fixture
def workflow_client_factory(db_session: Session):
    app.dependency_overrides[get_db] = lambda: db_session

    def make_client(user: User | None) -> TestClient:
        client = TestClient(app, follow_redirects=False)
        if user is not None:
            _, token = issue_session(db_session, user_id=user.id)
            db_session.flush()
            client.cookies.set(SESSION_COOKIE_NAME, token)
        return client

    try:
        yield make_client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _user(db: Session, role: str, *, active: bool = True) -> User:
    user = User(name=f"{role.title()} {uuid4().hex[:6]}", email=f"{uuid4()}@example.test", role=role, is_active=active)
    db.add(user)
    db.flush()
    return user


def _enquiry(db: Session, assignee: User | None = None, status: str = "new") -> Enquiry:
    suffix = uuid4().hex[:8]
    package = Package(
        package_code=f"WF-{suffix}", slug=f"workflow-{suffix}", title="Workflow package", status="published",
        duration_days=5, prices=[PackagePrice(season_name="All year", start_month=1, end_month=12,
                                               price_per_person=Decimal("100.00"))],
    )
    enquiry = Enquiry(package=package, assigned_to=assignee, customer_name="Workflow Traveller",
                      original_message="Please help with a trip.", status=status,
                      travel_start_date=date(2027, 1, 1), travel_end_date=date(2027, 1, 5),
                      travel_dates_flexible=False, adults=1, children=0, infants=0)
    db.add(enquiry)
    db.flush()
    return enquiry


def _form_values(response: TestClient, enquiry: Enquiry) -> tuple[str, str]:
    import re

    page = response.get(f"/admin/enquiries/{enquiry.id}")
    assert page.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    updated = re.search(r'name="expected_updated_at" value="([^"]+)"', page.text)
    assert csrf and updated
    return csrf.group(1), updated.group(1)


def test_admin_assignment_reassignment_and_unassignment_are_audited(db_session, workflow_client_factory):
    admin = _user(db_session, "admin")
    first, second = _user(db_session, "consultant"), _user(db_session, "consultant")
    enquiry = _enquiry(db_session)
    client = workflow_client_factory(admin)
    csrf, updated = _form_values(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/assignment", data={
        "csrf_token": csrf, "expected_updated_at": updated, "assigned_to_id": str(first.id),
    })
    assert response.status_code == 303
    db_session.refresh(enquiry)
    assert enquiry.assigned_to_id == first.id
    csrf, updated = _form_values(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/assignment", data={
        "csrf_token": csrf, "expected_updated_at": updated, "assigned_to_id": str(second.id),
    })
    assert response.status_code == 303
    db_session.refresh(enquiry)
    csrf, updated = _form_values(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/assignment", data={
        "csrf_token": csrf, "expected_updated_at": updated, "assigned_to_id": "",
    })
    assert response.status_code == 303
    activities = list(db_session.scalars(select(EnquiryActivity).where(EnquiryActivity.enquiry_id == enquiry.id)
                                         .order_by(EnquiryActivity.created_at)))
    assert [(e.from_assigned_to_id, e.to_assigned_to_id, e.actor_id) for e in activities] == [
        (None, first.id, admin.id), (first.id, second.id, admin.id), (second.id, None, admin.id)
    ]


def test_unchanged_assignment_does_not_append_duplicate_activity(db_session, workflow_client_factory):
    admin, consultant = _user(db_session, "admin"), _user(db_session, "consultant")
    enquiry = _enquiry(db_session, consultant)
    client = workflow_client_factory(admin)
    csrf, updated = _form_values(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/assignment", data={
        "csrf_token": csrf, "expected_updated_at": updated, "assigned_to_id": str(consultant.id),
    })
    assert response.status_code == 303
    assert db_session.scalar(select(func.count(EnquiryActivity.id)).where(EnquiryActivity.enquiry_id == enquiry.id)) == 0


def test_assignment_and_activity_roll_back_together_on_failure(db_session, monkeypatch):
    from app.services import enquiry_workflow
    from app.services.enquiry_activity import append_enquiry_activity as append_activity

    admin, consultant = _user(db_session, "admin"), _user(db_session, "consultant")
    enquiry = _enquiry(db_session)
    updated_at = enquiry.updated_at.isoformat()

    def append_then_fail(*args, **kwargs):
        append_activity(*args, **kwargs)
        raise RuntimeError("Injected failure after activity flush")

    monkeypatch.setattr(enquiry_workflow, "append_enquiry_activity", append_then_fail)
    with pytest.raises(RuntimeError):
        with db_session.begin_nested():
            enquiry_workflow.assign_enquiry(
                db_session,
                enquiry_id=enquiry.id,
                actor=admin,
                assignee_id=consultant.id,
                expected_updated_at=updated_at,
            )
    db_session.refresh(enquiry)
    assert enquiry.assigned_to_id is None
    assert db_session.scalar(select(func.count(EnquiryActivity.id)).where(EnquiryActivity.enquiry_id == enquiry.id)) == 0


@pytest.mark.parametrize("target", ["missing", "inactive", "admin"])
def test_assignment_rejects_invalid_assignees_without_activity(db_session, workflow_client_factory, target):
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session)
    bad = None if target == "missing" else _user(db_session, "consultant" if target == "inactive" else "admin",
                                                  active=target != "inactive")
    client = workflow_client_factory(admin)
    csrf, updated = _form_values(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/assignment", data={
        "csrf_token": csrf, "expected_updated_at": updated,
        "assigned_to_id": str(uuid4() if bad is None else bad.id),
    })
    assert response.status_code == 422
    assert db_session.scalar(select(func.count(EnquiryActivity.id)).where(EnquiryActivity.enquiry_id == enquiry.id)) == 0
    db_session.refresh(enquiry)
    assert enquiry.assigned_to_id is None


@pytest.mark.parametrize("role", ["editor", "consultant"])
def test_non_admin_cannot_assign(db_session, workflow_client_factory, role):
    user = _user(db_session, role)
    enquiry = _enquiry(db_session, user if role == "consultant" else None)
    client = workflow_client_factory(user)
    csrf, updated = _form_values(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/assignment", data={
        "csrf_token": csrf, "expected_updated_at": updated, "assigned_to_id": "",
    })
    assert response.status_code == 403


@pytest.mark.parametrize("old,new", [
    ("new", "in_progress"), ("new", "closed"), ("in_progress", "waiting_customer"),
    ("in_progress", "closed"), ("waiting_customer", "in_progress"),
    ("waiting_customer", "closed"), ("closed", "in_progress"),
])
def test_allowed_status_transitions_record_actor_and_old_new(db_session, workflow_client_factory, old, new):
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session, status=old)
    client = workflow_client_factory(admin)
    csrf, updated = _form_values(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/status", data={
        "csrf_token": csrf, "expected_updated_at": updated, "new_status": new,
    })
    assert response.status_code == 303
    db_session.refresh(enquiry)
    event = db_session.scalar(select(EnquiryActivity).where(EnquiryActivity.enquiry_id == enquiry.id))
    assert enquiry.status == new
    assert (event.actor_id, event.from_status, event.to_status) == (admin.id, old, new)


def test_invalid_status_transition_rolls_back_without_activity(db_session, workflow_client_factory):
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session, status="new")
    client = workflow_client_factory(admin)
    csrf, updated = _form_values(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/status", data={
        "csrf_token": csrf, "expected_updated_at": updated, "new_status": "waiting_customer",
    })
    assert response.status_code == 422
    db_session.refresh(enquiry)
    assert enquiry.status == "new"
    assert db_session.scalar(select(func.count(EnquiryActivity.id)).where(EnquiryActivity.enquiry_id == enquiry.id)) == 0


def test_consultant_can_update_only_assigned_enquiry(db_session, workflow_client_factory):
    consultant, other = _user(db_session, "consultant"), _user(db_session, "consultant")
    own, theirs = _enquiry(db_session, consultant), _enquiry(db_session, other)
    client = workflow_client_factory(consultant)
    csrf, updated = _form_values(client, own)
    assert client.post(f"/admin/enquiries/{own.id}/status", data={
        "csrf_token": csrf, "expected_updated_at": updated, "new_status": "in_progress",
    }).status_code == 303
    response = client.post(f"/admin/enquiries/{theirs.id}/status", data={
        "csrf_token": csrf, "expected_updated_at": updated, "new_status": "in_progress",
    })
    assert response.status_code == 404


def test_internal_note_records_author_and_does_not_leak_publicly(db_session, workflow_client_factory):
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session)
    client = workflow_client_factory(admin)
    csrf, updated = _form_values(client, enquiry)
    response = client.post(f"/admin/enquiries/{enquiry.id}/notes", data={
        "csrf_token": csrf, "expected_updated_at": updated, "note_text": "  PRIVATE staff note  ",
    })
    assert response.status_code == 303
    note = db_session.scalar(select(EnquiryNote).where(EnquiryNote.enquiry_id == enquiry.id))
    assert (note.author_id, note.note) == (admin.id, "PRIVATE staff note")
    from app.main import app as application
    public_response = TestClient(application).get(f"/packages/{enquiry.package.slug}")
    assert "PRIVATE staff note" not in public_response.text


def test_csrf_and_stale_form_requests_are_rejected(db_session, workflow_client_factory):
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session)
    client = workflow_client_factory(admin)
    csrf, updated = _form_values(client, enquiry)
    missing = client.post(f"/admin/enquiries/{enquiry.id}/notes", data={
        "expected_updated_at": updated, "note_text": "Should not save",
    })
    invalid = client.post(f"/admin/enquiries/{enquiry.id}/notes", data={
        "csrf_token": "wrong", "expected_updated_at": updated, "note_text": "Should not save",
    })
    assert missing.status_code == invalid.status_code == 403
    stale = client.post(f"/admin/enquiries/{enquiry.id}/status", data={
        "csrf_token": csrf, "expected_updated_at": "2020-01-01T00:00:00+00:00", "new_status": "closed",
    })
    assert stale.status_code == 409
    assert db_session.scalar(select(func.count(EnquiryNote.id)).where(EnquiryNote.enquiry_id == enquiry.id)) == 0


def test_unauthenticated_post_redirects_to_login(db_session, workflow_client_factory):
    enquiry = _enquiry(db_session)
    response = workflow_client_factory(None).post(f"/admin/enquiries/{enquiry.id}/notes", data={})
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
