"""Integration test for the public enquiry streaming request-size cap."""

import re
from sqlalchemy import func, select

from app.main import app
from app.database import get_db
from app.models import Enquiry
from fastapi.testclient import TestClient


def test_oversized_public_enquiry_is_rejected_before_submission(db_session, supplied_packages):
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        client = TestClient(app, follow_redirects=False)
        package = supplied_packages["kerala"]
        page = client.get(f"/packages/{package.slug}")
        nonce = re.search(r'name="form_nonce" value="([^"]+)"', page.text).group(1)
        response = client.post(
            f"/packages/{package.slug}/enquiries",
            headers={"origin": "http://testserver", "content-type": "application/x-www-form-urlencoded"},
            content=(f"form_nonce={nonce}&website=&original_message=" + ("x" * 17_000)).encode(),
        )
        assert response.status_code == 413
        assert db_session.scalar(select(func.count(Enquiry.id))) == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
