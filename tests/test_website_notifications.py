import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

import app.database as database_module
import app.main as main_module
from app.models.listing import Listing
from app.models.notification import Notification
from app.models.user import User
from app.services import notifications
from app.services.observer import (
    LISTING_PUBLISHED,
    NotificationEvent,
    notification_publisher,
)
from app.services.website_notifications import (
    WebsiteNotificationObserver,
    _is_duplicate_notification,
    register_website_observer,
    website_notification_observer,
)


@pytest.fixture(autouse=True)
def isolated_publisher():
    original = list(notification_publisher._observers)
    notification_publisher._observers.clear()
    yield
    notification_publisher._observers[:] = original


@pytest.fixture
def wired(test_session, monkeypatch):
    bound_engine = test_session.get_bind()
    monkeypatch.setattr("app.services.website_notifications.engine", bound_engine)
    monkeypatch.setattr("app.services.notifications.engine", bound_engine)
    return test_session


def add_user(session, email, *, verified=1, role="alumni"):
    user = User(
        username=email,
        email=email,
        password="x",
        role=role,
        email_verified=verified,
        display_name="Test",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_observer_selects_only_eligible_alumni(wired):
    eligible = add_user(wired, "eligible@my.uwi.edu")
    add_user(wired, "unverified@gmail.com", verified=0)
    add_user(wired, "admin@uwi.edu", role="admin")

    WebsiteNotificationObserver()(
        NotificationEvent(LISTING_PUBLISHED, listing_id=1, message="New job")
    )

    rows = wired.exec(select(Notification)).all()
    assert [row.user_id for row in rows] == [eligible.id]


def test_notifications_appear_in_existing_inbox_with_unchanged_shape(wired):
    user = add_user(wired, "alum@my.uwi.edu")

    WebsiteNotificationObserver()(
        NotificationEvent(LISTING_PUBLISHED, listing_id=2, message="New event")
    )

    inbox = notifications.inbox(user.id)
    assert len(inbox) == 1
    assert set(inbox[0]) == {"id", "user_id", "event_type", "message", "created_at"}
    assert inbox[0]["event_type"] == LISTING_PUBLISHED
    assert inbox[0]["message"] == "New event"


def test_reprocessing_same_event_does_not_duplicate_records(wired):
    user = add_user(wired, "alum@my.uwi.edu")
    event = NotificationEvent(LISTING_PUBLISHED, listing_id=3, message="Repeat")
    observer = WebsiteNotificationObserver()

    observer(event)
    observer(event)

    rows = wired.exec(
        select(Notification).where(Notification.event_id == event.event_id)
    ).all()
    assert len(rows) == 1
    assert rows[0].user_id == user.id
    indexes = {
        idx["name"]: idx
        for idx in sa_inspect(wired.get_bind()).get_indexes("notification")
    }
    user_event_index = indexes["uq_notification_user_event"]
    assert user_event_index["column_names"] == ["user_id", "event_id"]
    assert bool(user_event_index["unique"])


def test_user_event_index_rejects_direct_duplicate_insert(wired):
    user = add_user(wired, "alum@my.uwi.edu")
    values = {
        "user_id": user.id,
        "event_type": LISTING_PUBLISHED,
        "message": "First",
        "created_at": "now",
        "event_id": "direct-duplicate",
    }
    wired.add(Notification(**values))
    wired.commit()

    wired.add(Notification(**values))
    with pytest.raises(IntegrityError):
        wired.commit()
    wired.rollback()


def test_duplicate_conflict_does_not_block_other_recipients(wired):
    existing_user = add_user(wired, "existing@my.uwi.edu")
    other_user = add_user(wired, "other@my.uwi.edu")
    event = NotificationEvent(LISTING_PUBLISHED, listing_id=5, message="Conflict")
    wired.add(
        Notification(
            user_id=existing_user.id,
            event_type=event.event_type,
            message="Already delivered",
            created_at="earlier",
            event_id=event.event_id,
        )
    )
    wired.commit()

    WebsiteNotificationObserver()(event)

    rows = wired.exec(
        select(Notification).where(Notification.event_id == event.event_id)
    ).all()
    assert {row.user_id for row in rows} == {existing_user.id, other_user.id}
    existing = next(row for row in rows if row.user_id == existing_user.id)
    assert existing.message == "Already delivered"


class _FakePostgresError(Exception):
    def __init__(self, sqlstate: str | None, constraint_name: str | None):
        super().__init__("duplicate key value violates unique constraint")
        self.pgcode = sqlstate
        self.diag = SimpleNamespace(constraint_name=constraint_name)


def test_duplicate_detection_accepts_postgres_user_event_violation():
    original = _FakePostgresError(
        "23505", database_module._NOTIFICATION_EVENT_INDEX
    )

    assert _is_duplicate_notification(IntegrityError("INSERT", {}, original))


@pytest.mark.parametrize(
    ("sqlstate", "constraint_name"),
    [
        ("23505", "ix_notification_user_id"),
        ("23503", database_module._NOTIFICATION_EVENT_INDEX),
        (None, database_module._NOTIFICATION_EVENT_INDEX),
    ],
)
def test_duplicate_detection_rejects_non_matching_postgres_errors(
    sqlstate, constraint_name
):
    original = _FakePostgresError(sqlstate, constraint_name)

    assert not _is_duplicate_notification(IntegrityError("INSERT", {}, original))


def test_duplicate_detection_uses_specific_sqlite_message():
    duplicate = IntegrityError(
        "INSERT INTO notification",
        {},
        ValueError("UNIQUE constraint failed: notification.user_id, notification.event_id"),
    )
    unrelated = IntegrityError(
        "INSERT INTO notification",
        {},
        ValueError("NOT NULL constraint failed: notification.message"),
    )
    index_name_only = IntegrityError(
        "INSERT INTO notification",
        {},
        ValueError('duplicate key value violates "uq_notification_user_event"'),
    )

    assert _is_duplicate_notification(duplicate)
    assert not _is_duplicate_notification(unrelated)
    assert not _is_duplicate_notification(index_name_only)
    assert not _is_duplicate_notification(IntegrityError("INSERT", {}, None))


def test_unrelated_integrity_error_is_propagated(wired, monkeypatch):
    user = add_user(wired, "alum@my.uwi.edu")
    event = NotificationEvent(LISTING_PUBLISHED, listing_id=6, message="Failure")
    wired.add(
        Notification(
            user_id=user.id,
            event_type=event.event_type,
            message="Already delivered",
            created_at="earlier",
            event_id=event.event_id,
        )
    )
    wired.commit()
    monkeypatch.setattr(
        "app.services.website_notifications._is_duplicate_notification",
        lambda exc: False,
    )

    with pytest.raises(IntegrityError):
        WebsiteNotificationObserver()(event)


def test_repeated_registration_reuses_single_observer():
    register_website_observer()
    register_website_observer()

    assert notification_publisher._observers == [website_notification_observer]


def test_dispatch_through_shared_publisher_persists_notifications(wired):
    user = add_user(wired, "alum@my.uwi.edu")
    register_website_observer()
    event = NotificationEvent(LISTING_PUBLISHED, listing_id=4, message="Dispatch")

    notification_publisher.notify(event)

    assert len(notifications.inbox(user.id)) == 1
    assert notifications.inbox(user.id)[0]["message"] == "Dispatch"


def test_application_initialization_registers_observer(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.main.register_website_observer", lambda: calls.append("observer")
    )
    monkeypatch.setattr("app.seed.seed", lambda **kwargs: calls.append("seed"))

    async def run_lifespan():
        async with main_module.lifespan(FastAPI()):
            pass

    asyncio.run(run_lifespan())

    assert calls == ["observer", "seed"]


def test_existing_database_gains_event_id_and_unique_index(tmp_path, monkeypatch):
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    legacy_row = (7, 1, "listing.published", "legacy", "2026-01-01T00:00:00+00:00")
    with legacy_engine.begin() as conn:
        conn.execute(
            sa_text(
                "CREATE TABLE notification ("
                "id INTEGER PRIMARY KEY, user_id INTEGER, event_type VARCHAR, "
                "message VARCHAR, created_at VARCHAR)"
            )
        )
        conn.execute(
            sa_text(
                "INSERT INTO notification "
                "(id, user_id, event_type, message, created_at) "
                "VALUES (7, 1, 'listing.published', 'legacy', "
                "'2026-01-01T00:00:00+00:00')"
            )
        )
    monkeypatch.setattr(database_module, "engine", legacy_engine)

    database_module._ensure_notification_event_id_column()
    database_module._ensure_notification_event_id_column()

    with legacy_engine.connect() as conn:
        columns = {
            row[1] for row in conn.execute(sa_text("PRAGMA table_info(notification)"))
        }
        rows = conn.execute(
            sa_text(
                "SELECT id, user_id, event_type, message, created_at, event_id "
                "FROM notification"
            )
        ).all()
    indexes = {
        index["name"]: index
        for index in sa_inspect(legacy_engine).get_indexes("notification")
    }
    user_event_index = indexes["uq_notification_user_event"]

    assert "event_id" in columns
    assert user_event_index["column_names"] == ["user_id", "event_id"]
    assert bool(user_event_index["unique"])
    assert len(rows) == 1
    assert tuple(rows[0][:5]) == legacy_row
    assert rows[0][5] is None


@pytest.mark.skip(
    reason=(
        "Pending Kyle's #5.3: boards.publish_listing() must emit a "
        "NotificationEvent through the shared publisher before this can run"
    )
)
def test_publish_listing_triggers_website_observer(wired, monkeypatch):
    from app.services import boards

    bound_engine = wired.get_bind()
    monkeypatch.setattr("app.services.boards.engine", bound_engine)
    monkeypatch.setattr("app.services.board_auth.engine", bound_engine)
    admin = add_user(wired, "admin@uwi.edu", role="admin")
    alum = add_user(wired, "alum@my.uwi.edu")
    register_website_observer()

    titles = {}
    for kind in ("job", "event"):
        title = f"{kind.title()} From Boards"
        listing = Listing(
            author_id=admin.id,
            kind=kind,
            title=title,
            body="Body",
            status="pending",
            created_at="now",
        )
        wired.add(listing)
        wired.commit()
        wired.refresh(listing)
        titles[listing.id] = title

        result = boards.publish_listing(admin.id, listing.id)
        assert result["ok"], result
        assert result["listing"]["status"] == "published"

    inbox = notifications.inbox(alum.id)
    assert len(inbox) == 2
    assert {row["event_type"] for row in inbox} == {LISTING_PUBLISHED}
    for title in titles.values():
        assert any(title in row["message"] for row in inbox)
    assert notifications.inbox(admin.id) == []
