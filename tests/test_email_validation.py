import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.main import app
from app.database import engine
from app.models.user import User
from app.seed import seed
from app.services.accounts import (
    is_uwi_email,
    looks_like_uwi_email,
    normalize_email,
    require_verified_alumni,
    verify_email,
)
from app.utilities.security import encrypt_password


client = TestClient(app)


def setup_module() -> None:
    seed(reset=True)


@pytest.mark.parametrize(
    "email",
    [
        "student@uwi.edu",
        "student@my.uwi.edu",
        "student.name@uwi.edu",
        "Student@UWI.EDU",
        "student@MY.UWI.EDU",
    ],
)
def test_valid_uwi_email_addresses(email: str) -> None:
    assert is_uwi_email(email)
    assert looks_like_uwi_email(email)


@pytest.mark.parametrize(
    "email",
    [
        "student@gmail.com",
        "student@example.com",
        "student@uwi",
        "student",
        "",
        "@uwi.edu",
        "student@",
        "student@@uwi.edu",
    ],
)
def test_invalid_and_unrelated_email_addresses(email: str) -> None:
    assert not is_uwi_email(email)
    assert not looks_like_uwi_email(email)


@pytest.mark.parametrize(
    "email",
    [
        "student@uwi.edu.attacker.com",
        "student@attackeruwi.edu",
        "student@fakeuwi.edu",
        "student@my.uwi.edu.example.com",
        "student@uwi.edu@example.com",
        "student@uwi.edu.",
    ],
)
def test_deceptive_uwi_email_addresses_are_rejected(email: str) -> None:
    assert not is_uwi_email(email)
    assert not looks_like_uwi_email(email)


@pytest.mark.parametrize(
    ("email", "normalized"),
    [
        ("Student@UWI.Edu", "student@uwi.edu"),
        ("  Student.Name@MY.UWI.EDU  ", "student.name@my.uwi.edu"),
        ("Mixed.Case@UWI.EDU", "mixed.case@uwi.edu"),
    ],
)
def test_normalize_email_lowercases_and_trims(email: str, normalized: str) -> None:
    assert normalize_email(email) == normalized
    assert is_uwi_email(email)


def test_uppercase_registration_stores_lowercase_email() -> None:
    response = client.post(
        "/api/auth/register",
        json={
            "email": "New.Student@MY.UWI.EDU",
            "password": "new-password",
            "display_name": "New Student",
        },
    )

    assert response.status_code == 200
    assert response.json()["user"]["email"] == "new.student@my.uwi.edu"
    assert "password" not in response.json()["user"]
    assert "password_hash" not in response.json()["user"]


def test_administrative_user_response_never_contains_password_fields() -> None:
    response = client.get("/api/users")

    assert response.status_code == 200
    assert response.json()
    for user in response.json():
        assert "password" not in user
        assert "password_hash" not in user


def test_database_user_model_dump_never_contains_password() -> None:
    with Session(engine) as session:
        user = User(
            username="Serialized.User@UWI.EDU",
            email="Serialized.User@UWI.EDU",
            password=encrypt_password("serialized-password"),
            role="alumni",
            email_verified=1,
            display_name="Serialized User",
        )
        session.add(user)
        session.commit()
        session.refresh(user)

        dumped = user.model_dump()

    assert "password" not in dumped
    assert "password_hash" not in dumped


def test_duplicate_registration_is_rejected_case_insensitively() -> None:
    first = client.post(
        "/api/auth/register",
        json={
            "email": "Duplicate.Student@uwi.edu",
            "password": "first-password",
        },
    )
    second = client.post(
        "/api/auth/register",
        json={
            "email": "duplicate.student@UWI.EDU",
            "password": "second-password",
        },
    )

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["detail"] == "email already registered"


def test_login_accepts_different_email_capitalization() -> None:
    registered = client.post(
        "/api/auth/register",
        json={
            "email": "Case.Login@uwi.edu",
            "password": "case-password",
        },
    )
    assert registered.status_code == 200

    response = client.post(
        "/api/auth/login",
        json={
            "email": "cAsE.lOgIn@UwI.EdU",
            "password": "case-password",
        },
    )

    assert response.status_code == 200
    assert response.json()["user"]["email"] == "case.login@uwi.edu"
    assert "password" not in response.json()["user"]
    assert "password_hash" not in response.json()["user"]


def test_login_accepts_existing_mixed_case_account() -> None:
    with Session(engine) as session:
        user = User(
            username="Existing.Mixed@MY.UWI.EDU",
            email="Existing.Mixed@MY.UWI.EDU",
            password=encrypt_password("existing-password"),
            role="alumni",
            email_verified=1,
            display_name="Existing Mixed-Case User",
        )
        session.add(user)
        session.commit()

    response = client.post(
        "/api/auth/login",
        json={
            "email": "existing.mixed@my.uwi.edu",
            "password": "existing-password",
        },
    )

    assert response.status_code == 200
    assert response.json()["user"]["email"] == "Existing.Mixed@MY.UWI.EDU"
    assert "password" not in response.json()["user"]
    assert "password_hash" not in response.json()["user"]


@pytest.mark.parametrize(
    "email",
    [
        "student@gmail.com",
        "student@uwi.edu.attacker.com",
        "student@attackeruwi.edu",
    ],
)
def test_invalid_institutional_email_registration_is_rejected(email: str) -> None:
    response = client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "invalid-password",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "email must use an approved UWI domain"


def test_verification_accepts_existing_mixed_case_account() -> None:
    with Session(engine) as session:
        user = User(
            username="Verify.Mixed@UWI.EDU",
            email="Verify.Mixed@UWI.EDU",
            password=encrypt_password("verify-password"),
            role="alumni",
            email_verified=0,
            display_name="Mixed-Case Verification",
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        user_id = user.id

    result = verify_email(user_id)

    assert result["ok"] is True
    assert result["user"]["email_verified"] == 1
    assert "password" not in result["user"]
    assert "password_hash" not in result["user"]


def test_verification_rejects_unapproved_account() -> None:
    with Session(engine) as session:
        user = User(
            username="Invalid.Verify@gmail.com",
            email="Invalid.Verify@gmail.com",
            password=encrypt_password("verify-password"),
            role="alumni",
            email_verified=0,
            display_name="Invalid Verification",
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        user_id = user.id

    result = verify_email(user_id)

    assert result["ok"] is False
    assert result["error"] == "email is not an approved UWI institutional address"

    with Session(engine) as session:
        stored = session.get(User, user_id)
        assert stored.email_verified == 0


def test_verified_alumni_pass_eligibility_with_mixed_case_email() -> None:
    result = require_verified_alumni(
        {
            "role": "alumni",
            "email": "Verified.Alumni@MY.UWI.EDU",
            "email_verified": 1,
        }
    )

    assert result is None


def test_unverified_alumni_fail_eligibility() -> None:
    result = require_verified_alumni(
        {
            "role": "alumni",
            "email": "Pending.Alumni@uwi.edu",
            "email_verified": 0,
        }
    )

    assert result == "email not verified"


def test_alumni_with_unapproved_email_fail_eligibility() -> None:
    result = require_verified_alumni(
        {
            "role": "alumni",
            "email": "student@gmail.com",
            "email_verified": 1,
        }
    )

    assert result == "email is not an approved UWI institutional address"


def test_admin_remains_exempt_from_alumni_eligibility() -> None:
    result = require_verified_alumni(
        {
            "role": "admin",
            "email": "admin@gmail.com",
            "email_verified": 0,
        }
    )

    assert result is None
