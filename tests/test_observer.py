
import logging
import threading

import pytest

from app.services.observer import (
    CAMPAIGN_SENT,
    LISTING_PUBLISHED,
    NotificationEvent,
    NotificationObserver,
    NotificationPublisher,
    NotificationPublishResult,
)


class RecordingObserver:

    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def __call__(self, event: NotificationEvent) -> None:
        self.events.append(event)


class FailingObserver:

    def __init__(self, error: Exception | None = None) -> None:
        self.events: list[NotificationEvent] = []
        self.error = error or RuntimeError("observer failed")

    def __call__(self, event: NotificationEvent) -> None:
        self.events.append(event)
        raise self.error


def test_notify_returns_empty_result_when_no_observers() -> None:
    publisher = NotificationPublisher()

    result = publisher.notify(
        NotificationEvent(CAMPAIGN_SENT, campaign_id=3)
    )

    assert result.succeeded is False
    assert result.succeeded_observers == ()
    assert result.attempted_observers == ()
    assert result.failed_observers == ()


def test_notify_with_result_no_observers_has_empty_attempted_and_failed_lists() -> None:
    publisher = NotificationPublisher()

    result = publisher.notify_with_result(
        NotificationEvent(CAMPAIGN_SENT, campaign_id=3)
    )

    assert result.succeeded is False
    assert result.attempted_observers == ()
    assert result.failed_observers == ()


def test_notify_with_result_is_per_call_under_concurrent_publishing() -> None:
    publisher = NotificationPublisher()
    barrier = threading.Barrier(2)

    def selective_observer(event: NotificationEvent) -> None:
        if event.campaign_id == 101:
            raise RuntimeError("first call fails")

    publisher.subscribe(selective_observer)

    def publish(event: NotificationEvent) -> NotificationPublishResult:
        barrier.wait(timeout=2)
        return publisher.notify_with_result(event)

    first_event = NotificationEvent(CAMPAIGN_SENT, campaign_id=101)
    second_event = NotificationEvent(CAMPAIGN_SENT, campaign_id=202)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(publish, first_event)
        second = executor.submit(publish, second_event)
        first_result = first.result(timeout=3)
        second_result = second.result(timeout=3)

    assert first_result.succeeded is False
    assert second_result.succeeded is True
    assert len(first_result.attempted_observers) == 1
    assert len(second_result.attempted_observers) == 1
    assert first_result.attempted_observers == (selective_observer,)
    assert second_result.attempted_observers == (selective_observer,)
    assert len(first_result.failed_observers) == 1
    assert second_result.failed_observers == ()


def test_subscribe_registers_observer_for_dispatch() -> None:
    publisher = NotificationPublisher()
    observer = RecordingObserver()
    event = NotificationEvent(LISTING_PUBLISHED, listing_id=7, message="New job")

    publisher.subscribe(observer)
    publisher.notify(event)

    assert observer.events == [event]


def test_subscribe_twice_does_not_duplicate_delivery() -> None:
    publisher = NotificationPublisher()
    observer = RecordingObserver()
    publisher.subscribe(observer)
    publisher.subscribe(observer)

    publisher.notify(NotificationEvent(LISTING_PUBLISHED, listing_id=7))

    assert len(observer.events) == 1


def test_unsubscribe_stops_delivery_but_leaves_other_observers() -> None:
    publisher = NotificationPublisher()
    removed = RecordingObserver()
    remaining = RecordingObserver()
    publisher.subscribe(removed)
    publisher.subscribe(remaining)

    publisher.unsubscribe(removed)
    publisher.notify(NotificationEvent(CAMPAIGN_SENT, campaign_id=3))

    assert removed.events == []
    assert len(remaining.events) == 1


def test_unsubscribe_unknown_observer_is_a_noop() -> None:
    publisher = NotificationPublisher()
    observer = RecordingObserver()
    publisher.subscribe(observer)

    publisher.unsubscribe(RecordingObserver())
    publisher.notify(NotificationEvent(CAMPAIGN_SENT, campaign_id=3))

    assert len(observer.events) == 1


def test_notify_dispatches_one_stable_event_to_every_observer() -> None:
    publisher = NotificationPublisher()
    first = RecordingObserver()
    second = RecordingObserver()
    publisher.subscribe(first)
    publisher.subscribe(second)
    event = NotificationEvent(CAMPAIGN_SENT, campaign_id=3, message="Campaign: ...")

    result = publisher.notify(event)

    assert first.events == [event]
    assert second.events == [event]
    assert first.events[0] is second.events[0] is event
    assert isinstance(first, NotificationObserver)
    assert result.succeeded is True
    assert result.succeeded_observers == (first, second)
    assert result.attempted_observers == (first, second)
    assert result.failed_observers == ()


def test_failing_observer_does_not_block_other_observers(caplog) -> None:
    publisher = NotificationPublisher()
    before = RecordingObserver()
    failing = FailingObserver()
    after = RecordingObserver()
    event = NotificationEvent(LISTING_PUBLISHED, listing_id=7, message="New job")
    publisher.subscribe(before)
    publisher.subscribe(failing)
    publisher.subscribe(after)

    with caplog.at_level(logging.ERROR):
        result = publisher.notify(event)

    assert len(before.events) == 1 and before.events[0] is event
    assert len(failing.events) == 1 and failing.events[0] is event
    assert len(after.events) == 1 and after.events[0] is event
    assert result.succeeded is False
    assert result.succeeded_observers == (before, after)
    assert result.attempted_observers == (before, failing, after)
    assert result.failed_observers == (failing,)
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    message = record.getMessage()
    assert "Notification observer FailingObserver failed" in message
    assert f"event type {LISTING_PUBLISHED}" in message
    assert f"event_id={event.event_id}" in message


def test_two_failing_observers_followed_by_healthy_observer(caplog) -> None:
    publisher = NotificationPublisher()
    attempted: list[str] = []

    def first_failure(event: NotificationEvent) -> None:
        attempted.append("first")
        raise RuntimeError("first failure")

    def second_failure(event: NotificationEvent) -> None:
        attempted.append("second")
        raise RuntimeError("second failure")

    healthy = RecordingObserver()

    def healthy_observer(event: NotificationEvent) -> None:
        attempted.append("healthy")
        healthy(event)

    event = NotificationEvent(CAMPAIGN_SENT, campaign_id=3)
    publisher.subscribe(first_failure)
    publisher.subscribe(second_failure)
    publisher.subscribe(healthy_observer)

    with caplog.at_level(logging.ERROR):
        publisher.notify(event)

    assert attempted == ["first", "second", "healthy"]
    assert len(healthy.events) == 1 and healthy.events[0] is event
    assert len(caplog.records) == 2
    messages = [record.getMessage() for record in caplog.records]
    assert any(first_failure.__qualname__ in message for message in messages)
    assert any(second_failure.__qualname__ in message for message in messages)
    assert all(
        f"event type {CAMPAIGN_SENT}" in message
        and f"event_id={event.event_id}" in message
        for message in messages
    )


def test_repeated_dispatch_results_are_independent() -> None:
    publisher = NotificationPublisher()

    def sometimes_fails(event: NotificationEvent) -> None:
        if event.campaign_id == 101:
            raise RuntimeError("first call fails")

    publisher.subscribe(sometimes_fails)

    first = publisher.notify(NotificationEvent(CAMPAIGN_SENT, campaign_id=101))
    second = publisher.notify(NotificationEvent(CAMPAIGN_SENT, campaign_id=202))

    assert first.succeeded is False
    assert first.failed_observers == (sometimes_fails,)
    assert first.succeeded_observers == ()
    assert second.succeeded is True
    assert second.failed_observers == ()
    assert second.succeeded_observers == (sometimes_fails,)


def test_dispatch_uses_initial_observer_snapshot() -> None:
    publisher = NotificationPublisher()
    second = RecordingObserver()

    def remove_second(event: NotificationEvent) -> None:
        publisher.unsubscribe(second)

    publisher.subscribe(remove_second)
    publisher.subscribe(second)
    event = NotificationEvent(CAMPAIGN_SENT, campaign_id=3)

    result = publisher.notify(event)

    assert result.attempted_observers == (remove_second, second)
    assert result.succeeded_observers == (remove_second, second)
    assert result.failed_observers == ()
    assert second.events == [event]


def test_failing_observer_remains_subscribed_for_future_events(caplog) -> None:
    publisher = NotificationPublisher()
    failing = FailingObserver()
    after = RecordingObserver()
    publisher.subscribe(failing)
    publisher.subscribe(after)
    first = NotificationEvent(LISTING_PUBLISHED, listing_id=7)
    second = NotificationEvent(CAMPAIGN_SENT, campaign_id=3)

    with caplog.at_level(logging.ERROR):
        publisher.notify(first)
        publisher.notify(second)

    assert len(failing.events) == 2
    assert failing.events[0] is first and failing.events[1] is second
    assert len(after.events) == 2
    assert after.events[0] is first and after.events[1] is second


def test_event_contract_covers_both_event_types_and_identifiers() -> None:
    listing_event = NotificationEvent(LISTING_PUBLISHED, listing_id=11, message="Job")
    campaign_event = NotificationEvent(CAMPAIGN_SENT, campaign_id=4, message="Mail")

    assert listing_event.event_id and campaign_event.event_id
    assert listing_event.event_id != campaign_event.event_id
    assert listing_event.listing_id == 11 and listing_event.campaign_id is None
    assert campaign_event.campaign_id == 4 and campaign_event.listing_id is None
    assert listing_event.to_dict()["event_type"] == LISTING_PUBLISHED
    assert listing_event.to_dict()["listing_id"] == 11
    assert campaign_event.to_dict()["event_type"] == CAMPAIGN_SENT
    assert campaign_event.to_dict()["campaign_id"] == 4


@pytest.mark.parametrize(
    ("event_type", "identifiers"),
    [
        (LISTING_PUBLISHED, {}),
        (LISTING_PUBLISHED, {"campaign_id": 3}),
        (LISTING_PUBLISHED, {"listing_id": 7, "campaign_id": 3}),
        (CAMPAIGN_SENT, {}),
        (CAMPAIGN_SENT, {"listing_id": 7}),
        (CAMPAIGN_SENT, {"listing_id": 7, "campaign_id": 3}),
    ],
)
def test_event_rejects_wrong_identifier_combinations(
    event_type: str, identifiers: dict[str, int]
) -> None:
    with pytest.raises(ValueError):
        NotificationEvent(event_type, **identifiers)


def test_event_rejects_unsupported_event_type() -> None:
    with pytest.raises(ValueError):
        NotificationEvent("listing.rejected", listing_id=7)


def test_listing_event_to_dict_keeps_kind_metadata() -> None:
    listing_event = NotificationEvent(
        LISTING_PUBLISHED, listing_id=7, kind="job", message="New job"
    )
    campaign_event = NotificationEvent(CAMPAIGN_SENT, campaign_id=3)

    assert listing_event.to_dict()["kind"] == "job"
    assert "kind" not in campaign_event.to_dict()
