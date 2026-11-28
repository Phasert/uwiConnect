import re

import pytest
from csrf_test_helpers import csrf_token_from_html
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine
from app.main import app
from app.models.listing import Listing
from app.models.user import User
from app.seed import seed
from app.services import accounts, board_auth, boards
from app.utilities.security import create_access_token

ALUMNI_EMAIL = "alumni.verified@my.uwi.edu"
ADMIN_EMAIL = "admin@uwi.edu"
UNVERIFIED_EMAIL = "alumni.pending@gmail.com"
PASSWORD = "alumni-dev"


@pytest.fixture
def client():
    seed(reset=True)
    return TestClient(app)


def _user_id(email: str, password: str = PASSWORD) -> int:
    user = board_auth.login(email, password)
    assert user is not None
    return user["id"]


def _login(client: TestClient, email: str, password: str = PASSWORD) -> TestClient:
    csrf_token = csrf_token_from_html(client.get("/login").text)
    response = client.post(
        "/login",
        data={
            "username": email,
            "password": password,
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def _add_listing(author_id: int, *, title: str, kind: str, status: str) -> None:
    with Session(engine) as session:
        session.add(
            Listing(
                author_id=author_id,
                kind=kind,
                title=title,
                body="Test listing body.",
                status=status,
                created_at="2099-03-01T12:00:00+00:00",
            )
        )
        session.commit()


def test_dashboard_requires_login(client):
    response = client.get("/app/dashboard")
    assert response.status_code == 401


def test_dashboard_links_to_separate_submission_page(client):
    page = _login(client, ALUMNI_EMAIL).get("/app/dashboard")

    assert page.status_code == 200
    assert 'href="http://testserver/app/dashboard/submit">Create submission</a>' in page.text
    assert 'id="listingSubmissionForm"' not in page.text


def test_alumni_can_navigate_between_dashboard_and_boards(client):
    client = _login(client, ALUMNI_EMAIL)

    dashboard = client.get("/app/dashboard")
    assert dashboard.status_code == 200
    assert re.search(
        r'<a class="app-nav-link is-active" href="[^"]*/app/dashboard">My submissions</a>',
        dashboard.text,
    )
    assert re.search(r'href="[^"]*/app">Browse boards</a>', dashboard.text)

    boards = client.get("/app")
    assert boards.status_code == 200
    assert re.search(
        r'<a class="app-nav-link is-active" href="[^"]*/app">Browse boards</a>',
        boards.text,
    )
    assert re.search(r'href="[^"]*/app/dashboard">My submissions</a>', boards.text)


def test_dashboard_shows_own_listings_with_statuses(client):
    alum_id = _user_id(ALUMNI_EMAIL)
    _add_listing(alum_id, title="Published Alumni Event", kind="event", status="published")
    _add_listing(alum_id, title="Rejected Alumni Job", kind="job", status="rejected")

    page = _login(client, ALUMNI_EMAIL).get("/app/dashboard")

    assert page.status_code == 200
    assert "Pending Alumni Job" in page.text
    assert "Published Alumni Event" in page.text
    assert "Rejected Alumni Job" in page.text
    assert "Job" in page.text
    assert "Event" in page.text
    assert "Pending" in page.text
    assert "Published" in page.text
    assert "Rejected" in page.text


def test_dashboard_combines_keyword_type_and_status_filters(client):
    author_id = _user_id(ALUMNI_EMAIL)
    _add_listing(author_id, title="Research workshop", kind="event", status="published")
    _add_listing(author_id, title="Research vacancy", kind="job", status="published")
    _add_listing(author_id, title="Research seminar", kind="event", status="pending")
    _add_listing(_user_id(ADMIN_EMAIL, "admin-dev"), title="Research fair", kind="event", status="published")
    client = _login(client, ALUMNI_EMAIL)
    filters = {"q": "RESEARCH", "kind": "event", "listing_status": "published"}

    page = client.get("/app/dashboard", params=filters)

    assert page.status_code == 200
    assert "Research workshop" in page.text
    for title in ("Research vacancy", "Research seminar", "Research fair"):
        assert title not in page.text
    assert 'value="event" selected' in page.text
    assert 'value="published" selected' in page.text
    empty = client.get("/app/dashboard", params={**filters, "q": "no matching title"})
    assert "No submissions match your search." in empty.text


def test_dashboard_hides_other_users_listings(client):
    admin_id = _user_id(ADMIN_EMAIL, "admin-dev")
    with Session(engine) as session:
        other_id = session.exec(select(User.id).where(User.email == UNVERIFIED_EMAIL)).one()
    _add_listing(admin_id, title="Admin Secret Job", kind="job", status="published")
    _add_listing(
        other_id,
        title="Other Alumni Secret Event",
        kind="event",
        status="pending",
    )

    page = _login(client, ALUMNI_EMAIL).get(f"/app/dashboard?author_id={other_id}")

    assert page.status_code == 200
    assert "Pending Alumni Job" in page.text
    assert "Admin Secret Job" not in page.text
    assert "Other Alumni Secret Event" not in page.text


def test_dashboard_with_no_submissions_shows_message(client):
    result = accounts.register(
        "empty@my.uwi.edu", PASSWORD, "Empty Alum", "Computer Science", "FST"
    )
    assert result["ok"]
    accounts.verify_email(result["user"]["id"])

    page = _login(client, "empty@my.uwi.edu").get("/app/dashboard")

    assert page.status_code == 200
    assert "You have not submitted any listings yet." in page.text


def test_unverified_alumni_cannot_access_dashboard(client):
    with Session(engine) as session:
        user_id = session.exec(select(User.id).where(User.email == UNVERIFIED_EMAIL)).one()
    client.cookies.set("access_token", create_access_token({"sub": str(user_id)}))
    assert client.get("/app/dashboard").status_code == 401


def test_list_by_author_returns_only_that_authors_listings(client):
    alum_id = _user_id(ALUMNI_EMAIL)
    admin_id = _user_id(ADMIN_EMAIL, "admin-dev")

    own_titles = {row["title"] for row in boards.list_by_author(alum_id)}
    admin_titles = {row["title"] for row in boards.list_by_author(admin_id)}

    assert own_titles == {"Pending Alumni Job"}
    assert all(row["author_id"] == alum_id for row in boards.list_by_author(alum_id))
    assert "Seed Software Role" in admin_titles
    assert "Pending Alumni Job" not in admin_titles
