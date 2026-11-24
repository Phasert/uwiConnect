import pytest
from sqlmodel import select

import app.services.recipient_selection as selection_module
import app.services.website_notifications as website_module
from app.models.listing import Listing
from app.models.notification import Notification
from app.models.user import User
from app.services.observer import (
    CAMPAIGN_SENT,
    LISTING_PUBLISHED,
    NotificationEvent,
    NotificationPublisher,
)
from app.services.recipient_selection import (
    RecipientSelectionError,
    email_recipient_selector,
    select_recipients,
)
from app.services.website_notifications import WebsiteNotificationObserver


@pytest.fixture
def website_engine(test_session, monkeypatch):
    monkeypatch.setattr(website_module, "engine", test_session.get_bind())
    monkeypatch.setattr(selection_module, "engine", test_session.get_bind())


def _user(
    session,
    email: str,
    *,
    faculty: str | None = "Science and Technology",
    department: str | None = "Computer Science",
    verified: int = 1,
    role: str = "alumni",
    account_active: bool = True,
    account_revoked: bool = False,
) -> User:
    user = User(
        username=email,
        email=email,
        password="x",
        role=role,
        email_verified=verified,
        display_name="Test",
        programme="Computer Science",
        faculty=faculty,
        department=department,
        account_active=account_active,
        account_revoked=account_revoked,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _listing(
    session, *, faculty: str | None = None, department: str | None = None
) -> Listing:
    listing = Listing(
        author_id=1,
        kind="job",
        title="Targeted Role",
        body="Body",
        status="published",
        created_at="now",
        faculty=faculty,
        department=department,
    )
    session.add(listing)
    session.commit()
    session.refresh(listing)
    return listing


def _listing_event(listing_id: int) -> NotificationEvent:
    return NotificationEvent(
        LISTING_PUBLISHED, listing_id=listing_id, message="New job", event_id="sel-1"
    )


def _campaign_event() -> NotificationEvent:
    return NotificationEvent(
        CAMPAIGN_SENT, campaign_id=1, message="Campaign", event_id="sel-campaign"
    )


def _selected_ids(event: NotificationEvent, session) -> set[int]:
    return {user["id"] for user in select_recipients(event, session)}


def test_faculty_only_target_matches_faculty_and_ignores_department(
    test_session,
) -> None:
    matching = _user(test_session, "match@my.uwi.edu")
    other_faculty = _user(test_session, "other@my.uwi.edu", faculty="Engineering")
    listing = _listing(test_session, faculty="Science and Technology")

    assert _selected_ids(_listing_event(listing.id), test_session) == {matching.id}
    assert other_faculty.id not in _selected_ids(
        _listing_event(listing.id), test_session
    )


def test_department_only_target_matches_department_and_ignores_faculty(
    test_session,
) -> None:
    matching = _user(test_session, "match@my.uwi.edu", faculty="Engineering")
    other_department = _user(
        test_session, "other@my.uwi.edu", department="Electrical Engineering"
    )
    listing = _listing(test_session, department="Computer Science")

    assert _selected_ids(_listing_event(listing.id), test_session) == {matching.id}
    assert other_department.id not in _selected_ids(
        _listing_event(listing.id), test_session
    )


@pytest.mark.parametrize(
    (
        "target_faculty",
        "target_department",
        "candidate_faculty",
        "candidate_department",
    ),
    [
        (
            "Science and Technology",
            "Computer Science",
            "Engineering",
            "Computer Science",
        ),
        (
            "Science and Technology",
            "Computer Science",
            "Science and Technology",
            "Economics",
        ),
    ],
)
def test_combined_targets_require_both_values_to_match(
    test_session,
    target_faculty: str,
    target_department: str,
    candidate_faculty: str,
    candidate_department: str,
) -> None:
    qualified = _user(test_session, "qualified@my.uwi.edu")
    _user(
        test_session,
        "mismatch@my.uwi.edu",
        faculty=candidate_faculty,
        department=candidate_department,
    )
    listing = _listing(
        test_session, faculty=target_faculty, department=target_department
    )

    assert _selected_ids(_listing_event(listing.id), test_session) == {qualified.id}


@pytest.mark.parametrize(
    ("faculty", "department"),
    [
        (None, "Computer Science"),
        ("Science and Technology", None),
        ("  ", ""),
        (None, None),
    ],
)
def test_missing_or_blank_qualifications_exclude_recipient_from_all_listings(
    test_session, faculty: str | None, department: str | None
) -> None:
    qualified = _user(test_session, "qualified@my.uwi.edu")
    unqualified = _user(
        test_session, "unqualified@my.uwi.edu", faculty=faculty, department=department
    )
    targeted = _listing(
        test_session, faculty="Science and Technology", department="Computer Science"
    )
    untargeted = _listing(test_session)

    for listing in (targeted, untargeted):
        selected = _selected_ids(_listing_event(listing.id), test_session)
        assert qualified.id in selected
        assert unqualified.id not in selected


def test_existing_untargeted_listing_matches_qualified_recipients(test_session) -> None:
    qualified = _user(test_session, "qualified@my.uwi.edu")
    listing = _listing(test_session, faculty="  ", department=None)

    assert _selected_ids(_listing_event(listing.id), test_session) == {qualified.id}


def test_missing_listing_raises_before_any_notification(
    test_session, website_engine
) -> None:
    _user(test_session, "qualified@my.uwi.edu")

    with pytest.raises(RecipientSelectionError, match="999999"):
        WebsiteNotificationObserver()(_listing_event(999999))

    assert test_session.exec(select(Notification)).all() == []


@pytest.mark.parametrize(
    ("email", "kwargs"),
    [
        ("admin@uwi.edu", {"role": "admin"}),
        ("unverified@uwi.edu", {"verified": 0}),
        ("inactive@my.uwi.edu", {"account_active": False}),
        ("revoked@my.uwi.edu", {"account_revoked": True}),
        ("domain@example.com", {}),
    ],
)
def test_ineligible_accounts_are_excluded(test_session, email: str, kwargs) -> None:
    eligible = _user(test_session, "eligible@my.uwi.edu")
    _user(test_session, email, **kwargs)
    listing = _listing(test_session, faculty="Science and Technology")

    assert _selected_ids(_listing_event(listing.id), test_session) == {eligible.id}


def test_campaign_recipients_need_no_qualifications(test_session) -> None:
    no_qualifications = _user(
        test_session, "campaign@my.uwi.edu", faculty=None, department=None
    )
    _user(test_session, "revoked@my.uwi.edu", account_revoked=True)

    assert _selected_ids(_campaign_event(), test_session) == {no_qualifications.id}


def test_email_selector_reuses_the_shared_audience(
    test_session, website_engine
) -> None:
    qualified = _user(test_session, "qualified@my.uwi.edu")
    _user(test_session, "unqualified@my.uwi.edu", faculty=None)
    listing = _listing(test_session, faculty="Science and Technology")

    assert email_recipient_selector(_listing_event(listing.id)) == [qualified.email]


def test_replay_does_not_duplicate_website_notifications(
    test_session, website_engine
) -> None:
    qualified = _user(test_session, "qualified@my.uwi.edu")
    listing = _listing(test_session, faculty="Science and Technology")
    event = _listing_event(listing.id)
    observer = WebsiteNotificationObserver()

    observer(event)
    observer(event)

    rows = test_session.exec(
        select(Notification).where(Notification.event_id == event.event_id)
    ).all()
    assert [row.user_id for row in rows] == [qualified.id]


def test_selection_failure_is_visible_in_dispatch_without_blocking_others(
    test_session, website_engine
) -> None:
    _user(test_session, "qualified@my.uwi.edu")
    seen: list[NotificationEvent] = []

    def healthy_observer(event: NotificationEvent) -> None:
        seen.append(event)

    publisher = NotificationPublisher()
    publisher.subscribe(WebsiteNotificationObserver())
    publisher.subscribe(healthy_observer)
    event = _listing_event(999999)

    result = publisher.notify(event)

    assert result.succeeded is False
    assert len(result.failed_observers) == 1
    assert seen == [event]
    assert test_session.exec(select(Notification)).all() == []
