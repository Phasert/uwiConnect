from datetime import date

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models.listing import Listing
from app.routers import board
from app.services import boards
from app.services.observer import notification_publisher


@pytest.fixture
def expiry_database(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(boards, "engine", engine)
    monkeypatch.setattr(boards, "_local_today", lambda: date(2030, 6, 15))
    yield engine
    SQLModel.metadata.drop_all(engine)


def _add_listing(engine, **values) -> int:
    listing = Listing(
        author_id=values.pop("author_id", 2),
        kind=values.pop("kind"),
        title=values.pop("title"),
        body=values.pop("body", "Listing details"),
        status=values.pop("status", "published"),
        created_at="2030-01-01T00:00:00+00:00",
        **values,
    )
    with Session(engine) as session:
        session.add(listing)
        session.commit()
        session.refresh(listing)
        return listing.id


def test_job_expiry_boundaries_and_author_history(expiry_database):
    future_id = _add_listing(
        expiry_database,
        kind="job",
        title="Future Job",
        application_deadline="2030-06-16",
    )
    today_id = _add_listing(
        expiry_database,
        kind="job",
        title="Deadline Today",
        application_deadline="2030-06-15",
    )
    expired_id = _add_listing(
        expiry_database,
        kind="job",
        title="Expired Job",
        application_deadline="2030-06-14",
    )
    no_deadline_id = _add_listing(
        expiry_database,
        kind="job",
        title="Open Job",
    )
    _add_listing(
        expiry_database,
        kind="job",
        title="Expired Pending Job",
        status="pending",
        application_deadline="2030-06-14",
    )
    _add_listing(
        expiry_database,
        kind="job",
        title="Expired Rejected Job",
        status="rejected",
        application_deadline="2030-06-14",
    )

    public_ids = {row["id"] for row in boards.browse_published()}
    history_ids = {row["id"] for row in boards.list_by_author(2)}

    assert public_ids == {future_id, today_id, no_deadline_id}
    assert expired_id in history_ids


def test_event_expiry_uses_end_of_event_day(expiry_database):
    future_id = _add_listing(
        expiry_database,
        kind="event",
        title="Future Event",
        event_date="2030-06-16",
        event_time="09:00",
    )
    today_id = _add_listing(
        expiry_database,
        kind="event",
        title="Event Today",
        event_date="2030-06-15",
        event_time="08:00",
    )
    expired_id = _add_listing(
        expiry_database,
        kind="event",
        title="Past Event",
        event_date="2030-06-14",
        event_time="23:59",
    )
    legacy_id = _add_listing(
        expiry_database,
        kind="event",
        title="Legacy Event Without Date",
    )

    public_ids = {row["id"] for row in boards.browse_published()}
    history_ids = {row["id"] for row in boards.list_by_author(2)}

    assert public_ids == {future_id, today_id, legacy_id}
    assert expired_id in history_ids


def test_expired_listings_stay_hidden_from_search_filters_and_route_response(
    expiry_database,
):
    visible_id = _add_listing(
        expiry_database,
        kind="event",
        title="Engineering Expo Future",
        event_date="2030-06-16",
        event_time="10:00",
        category="Careers",
    )
    _add_listing(
        expiry_database,
        kind="event",
        title="Engineering Expo Past",
        event_date="2030-06-14",
        event_time="10:00",
        category="Careers",
    )

    response = board.browse(
        q="Engineering",
        kind="event",
        category="Careers",
    )

    assert [row["id"] for row in response["listings"]] == [visible_id]
    assert board.browse(q="Expo Past")["listings"] == []


def test_expiry_checks_do_not_publish_notifications(expiry_database):
    _add_listing(
        expiry_database,
        kind="job",
        title="Expired Quietly",
        application_deadline="2030-06-14",
    )
    events = []
    notification_publisher.subscribe(events.append)
    try:
        boards.browse_published()
    finally:
        notification_publisher.unsubscribe(events.append)

    assert events == []
