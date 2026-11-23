from fastapi.testclient import TestClient

from app.main import app
from app.seed import seed
from app.services.observer import notification_publisher


def setup_module() -> None:
    seed(reset=True)


client = TestClient(app)


class RecordingObserver:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


def _login(email: str, password: str) -> dict:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200
    return r.json()["user"]


def _submit(user_id: int, payload: dict) -> dict:
    r = client.post(
        "/api/boards/listings",
        json=payload,
        headers={"X-User-Id": str(user_id)},
    )
    assert r.status_code == 200
    return r.json()


def _publish(user_id: int, listing_id: int):
    return client.post(
        f"/api/admin/listings/{listing_id}/publish",
        headers={"X-User-Id": str(user_id)},
    )


def _reject(user_id: int, listing_id: int):
    return client.post(
        f"/api/admin/listings/{listing_id}/reject",
        headers={"X-User-Id": str(user_id)},
    )


def _board(params: dict | None = None) -> list:
    r = client.get("/api/boards", params=params)
    assert r.status_code == 200
    return r.json()["listings"]


def _web_login() -> None:
    r = client.post(
        "/login",
        data={
            "username": "alumni.verified@my.uwi.edu",
            "password": "alumni-dev",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303


def _published_pair(admin: dict, alum: dict, job: dict, event: dict) -> tuple[int, int]:
    job_id = _submit(alum["id"], job)["listing"]["id"]
    event_id = _submit(alum["id"], event)["listing"]["id"]
    assert _publish(admin["id"], job_id).status_code == 200
    assert _publish(admin["id"], event_id).status_code == 200
    return job_id, event_id


def test_required_fields_are_validated_and_new_listing_is_pending():
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    headers = {"X-User-Id": str(alum["id"])}

    empty_title = client.post(
        "/api/boards/listings",
        json={"kind": "job", "title": "", "body": "Body"},
        headers=headers,
    )
    assert empty_title.status_code == 400

    blank_body = client.post(
        "/api/boards/listings",
        json={"kind": "event", "title": "Real Title", "body": "   "},
        headers=headers,
    )
    assert blank_body.status_code == 400

    valid = _submit(
        alum["id"], {"kind": "job", "title": "Valid Title", "body": "Valid body"}
    )
    assert valid["listing"]["status"] == "pending"
    assert valid["listing"]["id"] not in [row["id"] for row in _board()]


def test_keyword_search_matches_title_and_body_case_insensitively():
    admin = _login("admin@uwi.edu", "admin-dev")
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")

    job_id, event_id = _published_pair(
        admin,
        alum,
        {"kind": "job", "title": "Python Developer", "body": "Remote role"},
        {"kind": "event", "title": "Developer Meetup", "body": "Workshop about PYTHON basics"},
    )

    rows = _board({"q": "PyThOn"})
    found = {row["id"] for row in rows}

    assert found == {job_id, event_id}
    assert all(row["status"] == "published" for row in rows)


def test_kind_filter_returns_only_that_type():
    admin = _login("admin@uwi.edu", "admin-dev")
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")

    job_id, event_id = _published_pair(
        admin,
        alum,
        {"kind": "job", "title": "Kubernetes Engineer", "body": "Cluster work"},
        {"kind": "event", "title": "Kubernetes Meetup", "body": "Cluster talk"},
    )

    events = _board({"kind": "event"})
    jobs = _board({"kind": "job"})

    assert event_id in [row["id"] for row in events]
    assert job_id not in [row["id"] for row in events]
    assert all(row["kind"] == "event" for row in events)
    assert all(row["kind"] == "job" for row in jobs)


def test_search_and_kind_filter_work_together():
    admin = _login("admin@uwi.edu", "admin-dev")
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")

    job_id, event_id = _published_pair(
        admin,
        alum,
        {"kind": "job", "title": "Quantum Computing Role", "body": "Research team"},
        {"kind": "event", "title": "Quantum Computing Seminar", "body": "Campus talk"},
    )

    matching_jobs = _board({"q": "quantum", "kind": "job"})
    matching_events = _board({"q": "quantum", "kind": "event"})

    assert [row["id"] for row in matching_jobs] == [job_id]
    assert [row["id"] for row in matching_events] == [event_id]


def test_pending_and_rejected_listings_stay_hidden_with_filters():
    admin = _login("admin@uwi.edu", "admin-dev")
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")

    pending_id = _submit(
        alum["id"],
        {"kind": "job", "title": "SecretPending Role", "body": "Not visible"},
    )["listing"]["id"]
    rejected_id = _submit(
        alum["id"],
        {"kind": "event", "title": "SecretRejected Meetup", "body": "Not visible"},
    )["listing"]["id"]
    assert _reject(admin["id"], rejected_id).status_code == 200

    unfiltered = [row["id"] for row in _board()]
    by_kind = [row["id"] for row in _board({"kind": "job"})]
    by_search = [row["id"] for row in _board({"q": "Secret"})]
    combined = [row["id"] for row in _board({"q": "Secret", "kind": "event"})]

    for ids in (unfiltered, by_kind, by_search, combined):
        assert pending_id not in ids
        assert rejected_id not in ids


def test_empty_search_results_return_empty_list():
    rows = _board({"q": "zzqqxxnomatch99"})
    assert rows == []

    combined = _board({"q": "zzqqxxnomatch99", "kind": "job"})
    assert combined == []


def test_search_does_not_emit_notification_events():
    observer = RecordingObserver()
    notification_publisher.subscribe(observer)
    try:
        _board()
        _board({"q": "Python"})
        _board({"kind": "event"})
        _board({"q": "Python", "kind": "job"})
        _board({"q": "zzqqxxnomatch99"})
    finally:
        notification_publisher.unsubscribe(observer)

    assert observer.events == []


def test_existing_browse_route_shape_is_unchanged():
    r = client.get("/api/boards")
    assert r.status_code == 200
    assert "listings" in r.json()
    assert isinstance(r.json()["listings"], list)

    filtered = client.get("/api/boards", params={"kind": "job"})
    assert filtered.status_code == 200
    assert "listings" in filtered.json()


def test_app_renders_cards_and_preserves_search_filters():
    admin = _login("admin@uwi.edu", "admin-dev")
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    _published_pair(
        admin,
        alum,
        {
            "kind": "job",
            "title": "Frontend Accessibility Role",
            "body": "Build accessible interfaces",
        },
        {
            "kind": "event",
            "title": "Frontend Networking Evening",
            "body": "Meet local developers",
        },
    )
    _web_login()

    page = client.get("/app", params={"q": "Frontend", "kind": "job"})

    assert page.status_code == 200
    assert "Frontend Accessibility Role" in page.text
    assert "Build accessible interfaces" in page.text
    assert "Frontend Networking Evening" not in page.text
    assert ">Job<" in page.text
    assert 'value="Frontend"' in page.text
    assert 'value="job" selected' in page.text

    jobs = client.get("/app", params={"kind": "job"})
    events = client.get("/app", params={"kind": "event"})

    assert "Frontend Accessibility Role" in jobs.text
    assert "Frontend Networking Evening" not in jobs.text
    assert "Frontend Networking Evening" in events.text
    assert "Frontend Accessibility Role" not in events.text


def test_app_hides_unpublished_listings_and_shows_empty_message():
    admin = _login("admin@uwi.edu", "admin-dev")
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    _submit(
        alum["id"],
        {"kind": "job", "title": "Hidden Frontend Pending", "body": "Private"},
    )
    rejected_id = _submit(
        alum["id"],
        {"kind": "event", "title": "Hidden Frontend Rejected", "body": "Private"},
    )["listing"]["id"]
    assert _reject(admin["id"], rejected_id).status_code == 200
    _web_login()

    page = client.get("/app", params={"q": "Hidden Frontend"})

    assert page.status_code == 200
    assert "Hidden Frontend Pending" not in page.text
    assert "Hidden Frontend Rejected" not in page.text
    assert "No listings found" in page.text
