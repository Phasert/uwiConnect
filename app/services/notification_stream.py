"""Best-effort database-backed SSE projection of committed notification rows.

This is not an outbox. The stream uses notification IDs as cursors and polls
``id > cursor``. Row IDs reflect ID allocation order, not necessarily commit
order, so a notification allocated a lower ID but committed after the cursor
advanced can be missed. This stream therefore does not provide gap-free or
at-least-once delivery; ``GET /api/notifications`` remains the authoritative
inbox snapshot.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import jwt
from fastapi import Request
from jwt.exceptions import InvalidTokenError
from sqlalchemy import func
from sqlmodel import Session, select

from app.config import get_settings
from app.database import engine
from app.models.notification import Notification
from app.models.user import User
from app.services.alumni_eligibility import is_account_accessible

logger = logging.getLogger(__name__)

# Bounded polling: each open stream runs one indexed query per check interval,
# with an additional comment frame at most once per heartbeat interval when idle.
STREAM_CHECK_INTERVAL_SECONDS = 1.0
STREAM_HEARTBEAT_SECONDS = 15.0
STREAM_BATCH_SIZE = 100

EVENT_NAME = "notification"
HEARTBEAT_FRAME = ": keep-alive\n\n"


def token_expiry_timestamp(token: str | None) -> float | None:
    """Return the validated token's expiry, without requiring it to be unexpired."""
    if not token:
        return None
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"verify_exp": False},
        )
    except InvalidTokenError:
        return None
    expiry = payload.get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        return None
    return float(expiry)


def _read_batch(
    *, user_id: int, cursor: int, batch_size: int
) -> tuple[bool, list[dict[str, Any]]]:
    """Return account accessibility and one user-scoped batch of committed rows."""
    with Session(engine) as session:
        user = session.get(User, user_id)
        if user is None or not is_account_accessible(user):
            return False, []
        rows = session.exec(
            select(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.id > cursor,
            )
            .order_by(Notification.id.asc())
            .limit(batch_size)
        ).all()
        return True, [row.model_dump() for row in rows]


def user_notification_high_water_mark(*, user_id: int) -> int:
    """Return the highest committed notification ID owned by ``user_id``."""
    with Session(engine) as session:
        highest = session.exec(
            select(func.max(Notification.id)).where(Notification.user_id == user_id)
        ).one()
        return int(highest or 0)


def _on_stream_closed() -> None:
    """Lifecycle hook retained as a no-op in production and replaced in tests."""


def _expired(expires_at: float | None) -> bool:
    if expires_at is None:
        return False
    return datetime.now(timezone.utc).timestamp() >= expires_at


def _frame_for(row: dict[str, Any]) -> str:
    notification_id = row["id"]
    data = json.dumps(row, separators=(",", ":"))
    return f"id: {notification_id}\nevent: {EVENT_NAME}\ndata: {data}\n\n"


async def iter_notification_events(
    *,
    request: Request,
    user_id: int,
    cursor: int,
    expires_at: float | None,
) -> AsyncIterator[str]:
    """Yield committed rows owned by ``user_id`` in ascending ID order."""
    loop = asyncio.get_running_loop()
    check_interval = max(STREAM_CHECK_INTERVAL_SECONDS, 0.01)
    heartbeat_interval = max(STREAM_HEARTBEAT_SECONDS, check_interval)
    last_activity = loop.time()

    try:
        while True:
            if _expired(expires_at):
                logger.info("Closing notification stream: JWT expired")
                break
            if await request.is_disconnected():
                break

            accessible, rows = await asyncio.to_thread(
                _read_batch,
                user_id=user_id,
                cursor=cursor,
                batch_size=STREAM_BATCH_SIZE,
            )
            if not accessible:
                logger.info("Closing notification stream: account inaccessible")
                break

            if rows:
                for row in rows:
                    if _expired(expires_at):
                        break
                    notification_id = row["id"]
                    cursor = notification_id
                    yield _frame_for(row)
                last_activity = loop.time()
                await asyncio.sleep(0)
                continue

            elapsed = loop.time() - last_activity
            if elapsed >= heartbeat_interval:
                yield HEARTBEAT_FRAME
                last_activity = loop.time()
                elapsed = 0.0

            wait = min(check_interval, max(heartbeat_interval - elapsed, 0.01))
            await asyncio.sleep(wait)
    finally:
        _on_stream_closed()
