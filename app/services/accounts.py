"""Registration and UWI-email verification (intentionally soft)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.database import engine
from app.models.user import User
from app.services.board_auth import public_user
from app.utilities.security import encrypt_password


APPROVED_UWI_DOMAINS = frozenset({"uwi.edu", "my.uwi.edu"})


def normalize_email(email: str) -> str:
    return email.strip().lower()


def is_uwi_email(email: str) -> bool:
    normalized_email = normalize_email(email)
    if not normalized_email or normalized_email.count("@") != 1:
        return False

    local_part, domain = normalized_email.rsplit("@", 1)
    if not local_part or not domain:
        return False

    return domain.lower() in APPROVED_UWI_DOMAINS


def looks_like_uwi_email(email: str) -> bool:
    return is_uwi_email(email)


def register(
    email: str, password: str, display_name: str, *, role: str = "alumni"
) -> dict[str, Any]:
    if role not in {"alumni", "admin"}:
        return {"ok": False, "error": "role must be alumni|admin"}

    normalized_email = normalize_email(email)
    if not is_uwi_email(normalized_email):
        return {
            "ok": False,
            "error": "email must use an approved UWI domain",
        }

    with Session(engine) as session:
        existing = session.exec(
            select(User).where(func.lower(User.email) == normalized_email)
        ).first()
        if existing:
            return {"ok": False, "error": "email already registered"}

        try:
            user = User(
                username=normalized_email,
                email=normalized_email,
                password=encrypt_password(password),
                role=role,
                email_verified=0,
                display_name=display_name,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
        except IntegrityError:
            session.rollback()
            return {"ok": False, "error": "email already registered"}
        except Exception as exc:
            session.rollback()
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "user": public_user(user)}


def verify_email(user_id: int) -> dict[str, Any]:
    """Mark email verified - demo stub (no real token mailer)."""
    with Session(engine) as session:
        user = session.get(User, user_id)
        if user is None:
            return {"ok": False, "error": "user not found"}

        normalized_email = normalize_email(user.email)
        if not is_uwi_email(normalized_email):
            return {
                "ok": False,
                "error": "email is not an approved UWI institutional address",
            }

        user.email_verified = 1
        session.add(user)
        session.commit()
        session.refresh(user)
        return {"ok": True, "user": public_user(user)}


def require_verified_alumni(user: dict[str, Any] | None) -> str | None:
    if not user:
        return "unknown user"
    if user["role"] != "alumni":
        return None  # admins bypass
    if not is_uwi_email(normalize_email(user.get("email", ""))):
        return "email is not an approved UWI institutional address"
    if not user.get("email_verified"):
        return "email not verified"
    return None
