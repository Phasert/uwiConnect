"""Issue and atomically consume email verification tokens.

The caller owns the session transaction and must commit or roll it back.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlmodel import Session

from app.models.user import User
from app.models.verification_token import VerificationToken


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_verification_token(
    session: Session, user_id: int, *, now: datetime | None = None
) -> str:
    if session.get(User, user_id) is None:
        raise ValueError("user not found")
    issued_at = now if now is not None else datetime.now(timezone.utc)
    expires_at = _utc_timestamp(issued_at + timedelta(minutes=30))
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
