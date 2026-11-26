"""Issue and atomically consume email verification tokens.

The caller owns the session transaction and must commit or roll it back.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlmodel import Session, select

from app.models.user import User
from app.models.verification_token import VerificationToken


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def lock_verification_user(session: Session, user_id: int) -> bool:
    # Serialize token issuance and consumption even on SQLite, which ignores FOR UPDATE.
    locked_user_id = session.execute(
        update(User)
        .where(User.id == user_id)
        .values(email_verified=User.email_verified)
        .returning(User.id)
    ).scalar_one_or_none()
    return locked_user_id is not None


def has_recent_verification_token(session: Session, user_id: int) -> bool:
    cutoff = _utc_timestamp(datetime.now(timezone.utc) + timedelta(minutes=20))
    return session.exec(
        select(VerificationToken.id).where(
            VerificationToken.user_id == user_id,
            VerificationToken.consumed_at.is_(None),
            VerificationToken.expires_at > cutoff,
        )
    ).first() is not None


def issue_verification_token(
    session: Session, user_id: int, *, now: datetime | None = None
) -> str:
    issued_at = now if now is not None else datetime.now(timezone.utc)
    issued_timestamp = _utc_timestamp(issued_at)
    expires_at = _utc_timestamp(issued_at + timedelta(minutes=30))
    if not lock_verification_user(session, user_id):
        raise ValueError("user not found")
    session.execute(
        update(VerificationToken)
        .where(
            VerificationToken.user_id == user_id,
            VerificationToken.consumed_at.is_(None),
        )
        .values(consumed_at=issued_timestamp)
    )
    token = secrets.token_urlsafe(32)
    session.add(
        VerificationToken(
            user_id=user_id,
            token_hash=hash_token(token),
            expires_at=expires_at,
        )
    )
    session.flush()
    return token


def consume_verification_token(
    session: Session, token: str, *, now: datetime | None = None
) -> int | None:
    consumed_at = _utc_timestamp(now if now is not None else datetime.now(timezone.utc))
    statement = (
        update(VerificationToken)
        .where(
            VerificationToken.token_hash == hash_token(token),
            VerificationToken.consumed_at.is_(None),
            VerificationToken.expires_at > consumed_at,
        )
        .values(consumed_at=consumed_at)
        .returning(VerificationToken.user_id)
    )
    return session.execute(statement).scalar_one_or_none()
