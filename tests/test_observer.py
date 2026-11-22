
import pytest

from app.services.observer import (
    CAMPAIGN_SENT,
    LISTING_PUBLISHED,
    NotificationEvent,
    NotificationObserver,
    NotificationPublisher,
)


class RecordingObserver:

    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def __call__(self, event: NotificationEvent) -> None:
        self.events.append(event)


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

    publisher.notify(event)

    assert first.events == [event]
    assert second.events == [event]
    assert first.events[0] is second.events[0] is event
    assert isinstance(first, NotificationObserver)


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
