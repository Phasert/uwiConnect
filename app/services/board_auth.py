"""Scenario auth helpers (email login + public user dicts)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlmodel import Session, select

from app.database import engine
from app.models.user import User
from app.services.alumni_eligibility import is_account_accessible
from app.utilities.security import verify_password


def public_user(user: User | dict | None) -> dict[str, Any] | None:
    if user is None:
        return None
    if isinstance(user, dict):
        data = dict(user)
    else:
        data = user.model_dump()
    data.pop("password", None)
    data.pop("password_hash", None)
    return data


def login(email: str, password: str) -> dict[str, Any] | None:
    from app.services.accounts import normalize_email

    normalized_email = normalize_email(email)
    with Session(engine) as session:
        user = session.exec(
            select(User).where(func.lower(User.email) == normalized_email)
        ).one_or_none()
        if (
            user is None
            or not is_account_accessible(user)
            or not verify_password(password, user.password)
            or not user.email_verified
        ):
            return None
        return public_user(user)


def get_user(user_id: int) -> dict[str, Any] | None:
    with Session(engine) as session:
        user = session.get(User, user_id)
        return public_user(user)
