"""Access control for featured opportunities and board browsing."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine
from app.main import app
from app.models.user import User
from app.seed import seed
from app.services import boards
from tests.listing_auth_helpers import (
    VERIFIED_ALUMNI_EMAIL,
    VERIFIED_ALUMNI_PASSWORD,
    login,
)

PREVIEW_MESSAGE = "Sign in or register to explore jobs and events"


def setup_module() -> None:
    seed(reset=True)


def _user_id(email: str) -> int:
    with Session(engine) as session:
        return session.exec(select(User).where(User.email == email)).one().id


def _publish_distinctive() -> None:
    result = boards.submit_listing(
        _user_id(VERIFIED_ALUMNI_EMAIL),
        kind="job",
        title="Distinctive Graduate Role",
        body="Only verified alumni may read this description.",
        company_name="Distinctive Partner",
        location="Mona",
        category="Engineering",
        application_instructions="Apply by email",
        faculty="Science and Technology",
        application_deadline="2099-12-31",
    )
    assert result.get("ok"), result
    assert boards.publish_listing(
        _user_id("admin@uwi.edu"), result["listing"]["id"]
    ).get("ok")


def test_anonymous_landing_shows_preview_without_listing_details():
    _publish_distinctive()

    page = TestClient(app).get("/")

    assert page.status_code == 200
    assert "Featured opportunities" in page.text
    assert PREVIEW_MESSAGE in page.text
    for detail in (
        "Distinctive Graduate Role",
        "Distinctive Partner",
        "Mona",
        "Engineering",
        "2099-12-31",
    ):
        assert detail not in page.text
    assert "/api/boards" not in page.text


def test_verified_alumni_sees_real_featured_opportunities():
    _publish_distinctive()
    client = TestClient(app)
    login(client, VERIFIED_ALUMNI_EMAIL, VERIFIED_ALUMNI_PASSWORD)

    page = client.get("/")

    assert page.status_code == 200
    assert "Distinctive Graduate Role" in page.text
    assert "Distinctive Partner" in page.text
    assert PREVIEW_MESSAGE not in page.text
    assert client.get("/api/boards").status_code == 200


@pytest.mark.parametrize(
    ("email", "password", "api_status", "app_status"),
    [
        ("alumni.pending@gmail.com", "alumni-dev", 403, 403),
        ("admin@uwi.edu", "admin-dev", 403, 403),
    ],
)
def test_ineligible_accounts_see_preview_and_cannot_browse(
    email, password, api_status, app_status
):
    _publish_distinctive()
    client = TestClient(app)
    login(client, email, password)

    page = client.get("/")

    assert page.status_code == 200
    assert PREVIEW_MESSAGE in page.text
    assert "Distinctive Graduate Role" not in page.text
    assert client.get("/api/boards").status_code == api_status
    assert client.get("/app").status_code == app_status


@pytest.mark.parametrize(
    ("account_active", "account_revoked"),
    [(False, False), (True, True)],
)
def test_inaccessible_account_loses_featured_and_board_access(
    account_active, account_revoked
):
    _publish_distinctive()
    client = TestClient(app)
    alum_id = login(client, VERIFIED_ALUMNI_EMAIL, VERIFIED_ALUMNI_PASSWORD)["id"]

    with Session(engine) as session:
        user = session.get(User, alum_id)
        original = (user.account_active, user.account_revoked)
        user.account_active = account_active
        user.account_revoked = account_revoked
        session.add(user)
        session.commit()

    try:
        page = client.get("/")

        assert page.status_code == 200
        assert PREVIEW_MESSAGE in page.text
        assert "Distinctive Graduate Role" not in page.text
        assert client.get("/api/boards").status_code == 401
        assert client.get("/app").status_code == 401
    finally:
        with Session(engine) as session:
            user = session.get(User, alum_id)
            user.account_active, user.account_revoked = original
            session.add(user)
            session.commit()
