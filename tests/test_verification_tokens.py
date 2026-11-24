import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app import database
from app.models.user import User
from app.models.verification_token import VerificationToken
from app.services.verification_tokens import (
    consume_verification_token,
    issue_verification_token,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def add_user(session: Session) -> int:
    user = User(
        username="token-user",
        email="token-user@my.uwi.edu",
        password="hashed-password",
        role="alumni",
        email_verified=0,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    assert user.id is not None
    return user.id


def test_issue_stores_only_hash_and_utc_expiry(test_session):
    user_id = add_user(test_session)
    local_now = NOW.astimezone(timezone(timedelta(hours=-4)))
    raw_token = issue_verification_token(test_session, user_id, now=local_now)
    test_session.commit()

    record = test_session.exec(select(VerificationToken)).one()
    assert record.user_id == user_id
    assert record.token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
    assert raw_token != record.token_hash
    assert record.expires_at == "2026-01-01T12:30:00.000000+00:00"
    assert "token_hash" not in record.model_dump()


def test_tokens_are_distinct_and_user_must_exist(test_session):
    user_id = add_user(test_session)
    first = issue_verification_token(test_session, user_id, now=NOW)
    second = issue_verification_token(test_session, user_id, now=NOW)
    assert first != second
    with pytest.raises(ValueError, match="user not found"):
        issue_verification_token(test_session, user_id + 1, now=NOW)


def test_consumption_is_single_use_and_rollback_restores_token(test_session):
    user_id = add_user(test_session)
    token = issue_verification_token(test_session, user_id, now=NOW)
    test_session.commit()

    assert consume_verification_token(test_session, "incorrect", now=NOW) is None
    assert consume_verification_token(test_session, token, now=NOW) == user_id
    assert consume_verification_token(test_session, token, now=NOW) is None
    test_session.rollback()
    assert consume_verification_token(test_session, token, now=NOW) == user_id
    test_session.commit()
    assert consume_verification_token(test_session, token, now=NOW) is None


def test_expiry_boundary_and_naive_time_rejection(test_session):
    user_id = add_user(test_session)
    first = issue_verification_token(test_session, user_id, now=NOW)
    second = issue_verification_token(test_session, user_id, now=NOW)
    test_session.commit()
    assert (
        consume_verification_token(
            test_session, first, now=NOW + timedelta(minutes=30, microseconds=-1)
        )
        == user_id
    )
    assert (
        consume_verification_token(
            test_session, second, now=NOW + timedelta(minutes=30)
        )
        is None
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        issue_verification_token(test_session, user_id, now=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        consume_verification_token(test_session, second, now=NOW.replace(tzinfo=None))


def test_concurrent_consumption_succeeds_once(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'token-test.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            user_id = add_user(session)
            token = issue_verification_token(session, user_id, now=NOW)
            session.commit()

        barrier = Barrier(2)

        def consume():
            with Session(engine) as session:
                barrier.wait(timeout=5)
                result = consume_verification_token(session, token, now=NOW)
                session.commit()
                return result

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: consume(), range(2)))
        assert results.count(user_id) == 1
        assert results.count(None) == 1
    finally:
        engine.dispose()


def test_existing_database_gains_token_table_without_changing_users(
    tmp_path, monkeypatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'existing-test.db'}")
    User.__table__.create(engine)
    try:
        with Session(engine) as session:
            user_id = add_user(session)
            user = session.get(User, user_id)
            user.email_verified = 1
            session.commit()

        monkeypatch.setattr(database, "engine", engine)
        database.create_db_and_tables()
        database.create_db_and_tables()

        with Session(engine) as session:
            user = session.get(User, user_id)
            assert user.email_verified == 1
            token = issue_verification_token(session, user_id, now=NOW)
            session.commit()
            assert consume_verification_token(session, token, now=NOW) == user_id
    finally:
        engine.dispose()
