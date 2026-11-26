"""Registration and UWI-email verification."""

from __future__ import annotations

import smtplib
from html import escape
from typing import Any
from urllib.parse import quote

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.database import engine
from app.models.user import User
from app.services.alumni_eligibility import (
    clean_qualification,
    is_account_accessible,
    is_eligible_alumni,
)
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
    email: str,
    password: str,
    display_name: str,
    programme: str,
    faculty: str | None = None,
    department: str | None = None,
    *,
    role: str = "alumni",
) -> dict[str, Any]:
    if role not in {"alumni", "admin"}:
        return {"ok": False, "error": "role must be alumni|admin"}

    normalized_email = normalize_email(email)
    if not is_uwi_email(normalized_email):
        return {
            "ok": False,
            "error": "email must use an approved UWI domain",
        }
    qualification, error = clean_qualification(programme, faculty, department)
    if role == "alumni" and error:
        return {"ok": False, "error": error}

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
                **qualification,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
        except IntegrityError:
            session.rollback()
            return {"ok": False, "error": "email already registered"}
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


def send_verification_email(user_id: int) -> None:
    """Issue a verification token and deliver it after registration completes."""
    from app.config import get_settings
    from app.services.email_service import SMTPEmailService
    from app.services.verification_tokens import (
        has_recent_verification_token,
        issue_verification_token,
        lock_verification_user,
    )

    with Session(engine) as session:
        if not lock_verification_user(session, user_id):
            return
        user = session.get(User, user_id, populate_existing=True)
        if (
            user is None
            or user.email_verified
            or not is_account_accessible(user)
            or not is_uwi_email(user.email)
        ):
            return
        if has_recent_verification_token(session, user_id):
            return
        token = issue_verification_token(session, user_id)
        recipient = user.email

        base_url = get_settings().app_base_url.rstrip("/")
        verification_url = f"{base_url}/api/auth/verify-email?token={quote(token, safe='')}"
        text_body = (
            "Verify your UWI Connect email address by opening this link:\n\n"
            f"{verification_url}\n\n"
            "This link expires in 30 minutes."
        )
        html_body = (
            "<p>Verify your UWI Connect email address by opening this link:</p>"
            f'<p><a href="{escape(verification_url, quote=True)}">Verify email</a></p>'
            "<p>This link expires in 30 minutes.</p>"
        )
        try:
            SMTPEmailService().send_email(
                recipient,
                "Verify your UWI Connect email address",
                text_body,
                html_body=html_body,
            )
        except (smtplib.SMTPException, OSError, ValueError):
            session.rollback()
            return
        session.commit()


def resend_verification_email(email: str) -> None:
    from app.services.verification_resend import claim_resend_recipient

    normalized_email = normalize_email(email)
    with Session(engine) as session:
        if not claim_resend_recipient(session, normalized_email):
            return
        user = session.exec(
            select(User).where(func.lower(User.email) == normalized_email)
        ).first()
        if user is None or user.id is None:
            return
        user_id = user.id
    send_verification_email(user_id)


def verify_email_token(token: str) -> dict[str, Any]:
    """Consume a valid token and mark its associated account verified."""
    from app.models.verification_token import VerificationToken
    from app.services.verification_tokens import (
        consume_verification_token,
        hash_token,
        lock_verification_user,
    )

    with Session(engine) as session:
        candidate_id = session.exec(
            select(VerificationToken.user_id).where(
                VerificationToken.token_hash == hash_token(token)
            )
        ).first()
        if candidate_id is None or not lock_verification_user(session, candidate_id):
            session.rollback()
            return {"ok": False, "error": "verification token is invalid or expired"}
        user_id = consume_verification_token(session, token)
        if user_id is None:
            session.rollback()
            return {"ok": False, "error": "verification token is invalid or expired"}

        user = session.get(User, user_id)
        if user is None or not is_uwi_email(user.email):
            session.rollback()
            return {"ok": False, "error": "verification token is invalid or expired"}

        user.email_verified = 1
        session.add(user)
        session.commit()
        session.refresh(user)
        return {"ok": True, "user": public_user(user)}


def require_verified_alumni(user: dict[str, Any] | None) -> str | None:
    if not user:
        return "unknown user"
    if user["role"] != "alumni":
        return None if is_account_accessible(user) else "account inactive"
    if not is_uwi_email(normalize_email(user.get("email", ""))):
        return "email is not an approved UWI institutional address"
    if not user.get("email_verified"):
        return "email not verified"
    if user.get("account_revoked"):
        return "account revoked"
    if not user.get("account_active", True):
        return "account inactive"
    if not is_eligible_alumni(user):
        return "alumni account is not eligible"
    return None
