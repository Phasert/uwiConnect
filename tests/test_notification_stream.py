import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from queue import Empty, Queue
from threading import Thread
from typing import Any
from uuid import uuid4

import httpx
import pytest
from anyio import ClosedResourceError
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session, select

from app.database import engine
from app.main import app
from app.models.notification import Notification
from app.models.user import User
from app.seed import seed
from app.services import notification_stream
from app.services.observer import LISTING_PUBLISHED
from app.utilities.security import create_access_token
from tests.listing_auth_helpers import login

STREAM_URL = "/api/notifications/stream"
OWNER_EMAIL = "alumni.verified@my.uwi.edu"
OTHER_EMAIL = "alumni.pending@gmail.com"
PASSWORD = "alumni-dev"
INBOX_FIELDS = {"id", "user_id", "event_type", "message", "created_at"}


def setup_module() -> None:
    seed(reset=True)


client = TestClient(app)


@pytest.fixture(autouse=True)
def fast_stream(monkeypatch) -> None:
    monkeypatch.setattr(notification_stream, "STREAM_CHECK_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(notification_stream, "STREAM_HEARTBEAT_SECONDS", 0.1)
    monkeypatch.setattr(notification_stream, "STREAM_BATCH_SIZE", 2)


@pytest.fixture(autouse=True)
def clean_notifications() -> None:
    with Session(engine) as session:
        session.exec(delete(Notification))
        session.commit()
    yield


@pytest.fixture(scope="module")
def users() -> dict[str, int]:
    with Session(engine) as session:
        owner = session.exec(select(User).where(User.email == OWNER_EMAIL)).one()
        other = session.exec(select(User).where(User.email == OTHER_EMAIL)).one()
        assert owner.id is not None
        assert other.id is not None
        return {"owner": owner.id, "other": other.id}


def _login(email: str) -> dict:
    client.cookies.clear()
    return login(client, email, PASSWORD)


def _set_token(token: str | None) -> None:
    client.cookies.clear()
    if token is not None:
        client.cookies.set("access_token", token)


def _add_notification(user_id: int, message: str) -> int:
    with Session(engine) as session:
        row = Notification(
            user_id=user_id,
            event_type=LISTING_PUBLISHED,
            message=message,
            created_at="2026-11-24T00:00:00+00:00",
            event_id=f"stream-{uuid4().hex}",
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        assert row.id is not None
        return row.id


def _frame_from_lines(lines: list[str]) -> dict[str, Any] | None:
    event = None
    data = None
    event_id = None
    for line in lines:
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            continue
        value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data = value
    if event is None or data is None:
        return None
    return {"event": event, "id": event_id, "data": json.loads(data)}


class _StreamReader:
    """Read one httpx streaming response through an externally bounded queue."""

    _END = object()
    _EXPECTED_CLOSE_ERRORS = (
        ClosedResourceError,
        httpx.HTTPError,
        OSError,
        RuntimeError,
    )

    def __init__(self, response) -> None:
        self._response = response
        self._lines: Queue[Any] = Queue()
        self._thread = Thread(
            target=self._pump, daemon=True, name="notification-sse-reader"
        )
        self._thread.start()

    def _pump(self) -> None:
        try:
            for line in self._response.iter_lines():
                self._lines.put(line)
        except Exception as exc:  # noqa: BLE001 - forwarded to the owning test
            # Forward stream failures to the owning test instead of letting
            # them escape as unhandled thread exceptions.
            self._lines.put(exc)
        finally:
            self._lines.put(self._END)

    def _next_line(self, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for stream line")
            try:
                item = self._lines.get(timeout=remaining)
            except Empty as exc:
                raise TimeoutError("timed out waiting for stream line") from exc
            if item is self._END:
                return None
            if isinstance(item, Exception):
                raise item
            return item

    def close(self) -> None:
        try:
            self._response.close()
        except self._EXPECTED_CLOSE_ERRORS:
            pass
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            raise AssertionError("notification-sse-reader thread did not stop")

        while True:
            try:
                item = self._lines.get_nowait()
            except Empty:
                break
            if isinstance(item, Exception) and not isinstance(
                item, self._EXPECTED_CLOSE_ERRORS
            ):
                raise item

    def collect(
        self, *, count: int | None = None, wait: float = 2.0
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        lines: list[str] = []
        deadline = time.monotonic() + wait

        while True:
            if count is not None and len(events) >= count:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                line = self._next_line(remaining)
            except TimeoutError:
                break
            if line is None:
                break
            if line == "":
                frame = _frame_from_lines(lines)
                lines = []
                if frame is not None and frame["event"] == "notification":
                    events.append(frame)
            else:
                lines.append(line)

        return events

    def read_events(self, count: int, *, timeout: float = 2.0) -> list[dict[str, Any]]:
        events = self.collect(count=count, wait=timeout)
        assert len(events) == count, (
            f"expected {count} notification events, got {len(events)}"
        )
        return events

    def read_line(self, *, timeout: float = 2.0) -> str | None:
        try:
            return self._next_line(timeout)
        except TimeoutError as exc:
            raise AssertionError("timed out waiting for stream line") from exc

    def read_until_closed(self, *, timeout: float = 3.0) -> int:
        deadline = time.monotonic() + timeout
        lines = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("stream did not close before the timeout")
            try:
                line = self._next_line(remaining)
            except TimeoutError as exc:
                raise AssertionError("stream did not close before the timeout") from exc
            if line is None:
                return lines
            lines += 1


@contextmanager
def _open_stream(*, last_event_id: str | int | None = None):
    headers = {}
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    with client.stream("GET", STREAM_URL, headers=headers) as response:
        reader = _StreamReader(response) if response.status_code == 200 else None
        try:
            yield response, reader
        finally:
            if reader is not None:
                reader.close()


@pytest.mark.parametrize("credential", ["missing", "header_only", "invalid", "expired"])
def test_stream_rejects_unauthenticated_before_streaming(
    users, credential: str
) -> None:
    client.cookies.clear()
    headers = {}
    if credential == "header_only":
        headers["X-User-Id"] = str(users["owner"])
    elif credential == "invalid":
        _set_token("not-a-jwt")
    elif credential == "expired":
        _set_token(
            create_access_token(
                {"sub": str(users["owner"])}, expires_delta=timedelta(seconds=-1)
            )
        )

    with _open_stream() as (response, _reader):
        assert response.status_code == 401


@pytest.mark.parametrize(
    "cursor",
    ["0", "-1", "abc", "1.5", str(2**63)],
)
def test_invalid_last_event_id_is_rejected(users, cursor: str) -> None:
    _login(OWNER_EMAIL)

    with _open_stream(last_event_id=cursor) as (response, _reader):
        assert response.status_code == 400


def test_very_long_last_event_id_is_rejected(users) -> None:
    _login(OWNER_EMAIL)

    with _open_stream(last_event_id="9" * 5000) as (response, _reader):
        assert response.status_code == 400


def test_initial_connection_replays_committed_rows_ascending(users) -> None:
    owner_id = users["owner"]
    _login(OWNER_EMAIL)
    ids = [_add_notification(owner_id, f"owner-{index}") for index in range(3)]
    _add_notification(users["other"], "other-not-visible")

    with _open_stream() as (response, reader):
        assert response.status_code == 200
        assert reader is not None
        assert response.headers["content-type"].startswith("text/event-stream")
        events = reader.read_events(count=3)

    assert [event["data"]["id"] for event in events] == ids
    assert [event["id"] for event in events] == [str(value) for value in ids]
    assert [event["data"]["message"] for event in events] == [
        "owner-0",
        "owner-1",
        "owner-2",
    ]
    assert all(event["data"]["user_id"] == owner_id for event in events)
    assert all(set(event["data"]) == INBOX_FIELDS for event in events)


def test_stream_never_emits_other_users_rows(users) -> None:
    owner_id = users["owner"]
    _login(OWNER_EMAIL)
    _add_notification(users["other"], "other-one")
    _add_notification(users["other"], "other-two")
    owner_id = _add_notification(owner_id, "mine")

    with _open_stream() as (response, reader):
        assert response.status_code == 200
        assert reader is not None
        events = reader.read_events(count=1)
        later_events = reader.collect(wait=0.25)

    assert [event["data"]["id"] for event in events] == [owner_id]
    assert later_events == []


def test_reconnect_replays_only_rows_after_last_event_id(users) -> None:
    owner_id = users["owner"]
    _login(OWNER_EMAIL)
    _add_notification(owner_id, "one")
    checkpoint = _add_notification(owner_id, "two")
    third = _add_notification(owner_id, "three")
    fourth = _add_notification(owner_id, "four")

    with _open_stream(last_event_id=checkpoint) as (response, reader):
        assert response.status_code == 200
        assert reader is not None
        events = reader.read_events(count=2)

    assert [event["data"]["id"] for event in events] == [third, fourth]


def test_large_backlog_is_delivered_in_ascending_batches(users) -> None:
    owner_id = users["owner"]
    _login(OWNER_EMAIL)
    ids = [_add_notification(owner_id, f"batch-{index}") for index in range(5)]

    with _open_stream() as (response, reader):
        assert response.status_code == 200
        assert reader is not None
        events = reader.read_events(count=5)

    delivered = [event["data"]["id"] for event in events]
    assert delivered == ids
    assert len(set(delivered)) == 5


def test_new_commit_is_delivered_during_active_stream(users) -> None:
    owner_id = users["owner"]
    _login(OWNER_EMAIL)

    with _open_stream() as (response, reader):
        assert response.status_code == 200
        assert reader is not None
        assert reader.collect(wait=0.15) == []

        notification_id = _add_notification(owner_id, "delivered-while-open")
        events = reader.read_events(count=1, timeout=2.0)

    assert [event["data"]["id"] for event in events] == [notification_id]
    assert events[0]["data"]["message"] == "delivered-while-open"


def test_future_last_event_id_is_rejected_without_cross_user_disclosure(users) -> None:
    _login(OWNER_EMAIL)
    owner_id = _add_notification(users["owner"], "owner-latest")
    other_id = _add_notification(users["other"], "other-secret")
    assert other_id > owner_id

    with _open_stream(last_event_id=other_id) as (response, _reader):
        assert response.status_code == 409
        assert not response.headers["content-type"].startswith("text/event-stream")
        response.read()
        assert response.json() == {
            "detail": (
                "Last-Event-ID is ahead of notification stream; "
                "resynchronise from /api/notifications"
            )
        }
        assert "other-secret" not in response.text


def test_cursor_validation_uses_only_authenticated_users_rows(users) -> None:
    owner_id = users["owner"]
    _login(OWNER_EMAIL)
    checkpoint = _add_notification(owner_id, "owner-checkpoint")
    other_id = _add_notification(users["other"], "other-high")
    assert other_id > checkpoint

    with _open_stream(last_event_id=checkpoint) as (response, reader):
        assert response.status_code == 200
        assert reader is not None
        events = reader.collect(wait=0.25)

    assert events == []


def test_stream_closes_when_jwt_expires(users, monkeypatch) -> None:
    token = create_access_token(
        {"sub": str(users["owner"])}, expires_delta=timedelta(seconds=60)
    )
    expires_at = notification_stream.token_expiry_timestamp(token)
    assert expires_at is not None

    class _Clock:
        def __init__(self, value: datetime) -> None:
            self.value = value

        def now(self, tz=None) -> datetime:
            return self.value

    clock = _Clock(datetime.fromtimestamp(expires_at - 1, tz=timezone.utc))
    monkeypatch.setattr(notification_stream, "datetime", clock)
    _set_token(token)

    with _open_stream() as (response, reader):
        assert response.status_code == 200
        assert reader is not None
        assert (reader.read_line(timeout=1.0) or "").strip() == ": keep-alive"

        clock.value = datetime.fromtimestamp(expires_at + 1, tz=timezone.utc)
        reader.read_until_closed(timeout=2.0)


def test_stream_closes_when_account_access_is_revoked(users) -> None:
    owner_id = users["owner"]
    _login(OWNER_EMAIL)
    original: tuple[bool, bool] | None = None

    try:
        with _open_stream() as (response, reader):
            assert response.status_code == 200
            assert reader is not None
            assert (reader.read_line(timeout=1.0) or "").strip() == ": keep-alive"

            with Session(engine) as session:
                user = session.get(User, owner_id)
                assert user is not None
                original = (user.account_active, user.account_revoked)
                user.account_active = True
                user.account_revoked = True
                session.add(user)
                session.commit()

            reader.read_until_closed(timeout=2.0)
    finally:
        if original is not None:
            with Session(engine) as session:
                user = session.get(User, owner_id)
                assert user is not None
                user.account_active, user.account_revoked = original
                session.add(user)
                session.commit()


def test_client_disconnect_runs_stream_cleanup(users, monkeypatch) -> None:
    closed: list[bool] = []
    monkeypatch.setattr(
        notification_stream, "_on_stream_closed", lambda: closed.append(True)
    )
    _login(OWNER_EMAIL)

    with _open_stream() as (response, reader):
        assert response.status_code == 200
        assert reader is not None
        assert (reader.read_line(timeout=1.0) or "").strip() == ": keep-alive"

        notification_id = _add_notification(users["owner"], "deliver-then-disconnect")
        events = reader.read_events(count=1, timeout=2.0)

    assert [event["data"]["id"] for event in events] == [notification_id]

    deadline = time.monotonic() + 1.0
    while not closed and time.monotonic() < deadline:
        time.sleep(0.01)

    assert closed
