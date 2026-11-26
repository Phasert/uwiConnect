"""Bound verification resend requests by client and recipient."""

import hashlib
import hmac
from collections import deque
from threading import Lock
from time import monotonic, time

from fastapi import HTTPException, Request, status
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from app.config import get_settings
from app.models.verification_resend_limit import VerificationResendLimit

_WINDOW_SECONDS = 600
_MAX_REQUESTS = 5
_MAX_CLIENTS = 4096
_RECIPIENT_WINDOW_SECONDS = 600
_RECIPIENT_RETENTION_SECONDS = 86400
_attempts: dict[str, deque[float]] = {}
_lock = Lock()

RESEND_MESSAGE = (
    "If an eligible account exists, a verification email will be sent."
)


def check_resend_rate_limit(request: Request) -> None:
    client = request.client.host if request.client else "unknown"
    with _lock:
        now = monotonic()
        expired = [
            key for key, times in _attempts.items()
            if not times or times[-1] <= now - _WINDOW_SECONDS
        ]
        for key in expired:
            del _attempts[key]

        times = _attempts.get(client)
        if times is None:
            if len(_attempts) >= _MAX_CLIENTS:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many verification requests. Try again later.",
                )
            times = _attempts[client] = deque()
        while times and times[0] <= now - _WINDOW_SECONDS:
            times.popleft()
        if len(times) >= _MAX_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many verification requests. Try again later.",
            )
        times.append(now)


def claim_resend_recipient(session: Session, email: str) -> bool:
    recipient_hash = hmac.new(
        get_settings().secret_key.encode("utf-8"),
        email.strip().lower().encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    now = time()
    session.execute(
        delete(VerificationResendLimit).where(
            VerificationResendLimit.requested_at < now - _RECIPIENT_RETENTION_SECONDS
        )
    )
    session.commit()
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(VerificationResendLimit)
    elif dialect == "sqlite":
        statement = sqlite_insert(VerificationResendLimit)
    else:
        raise RuntimeError(f"Unsupported database for resend rate limiting: {dialect}")
    statement = statement.values(recipient_hash=recipient_hash, requested_at=now)
    statement = statement.on_conflict_do_update(
        index_elements=[VerificationResendLimit.recipient_hash],
        set_={"requested_at": now},
        where=VerificationResendLimit.requested_at <= now - _RECIPIENT_WINDOW_SECONDS,
    ).returning(VerificationResendLimit.recipient_hash)
    claimed = session.execute(statement).scalar_one_or_none() is not None
    session.commit()
    return claimed
