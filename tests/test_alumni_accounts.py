from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlmodel import Session, create_engine, select

from app import database
from app.main import app
from app.models.user import User
from app.seed import seed
from app.services.alumni_eligibility import is_eligible_alumni
from tests.listing_auth_helpers import login


def setup_module() -> None:
    seed(reset=True)


def test_registration_stores_trimmed_alumni_qualification() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/auth/register",
        json={
            "email": "qualified.alumni@my.uwi.edu",
            "password": "qualified-password",
            "display_name": "Qualified Alumni",
            "programme": "  Computer Science  ",
            "faculty": "  Faculty of Science and Technology  ",
            "department": "  Computing and Information Technology  ",
        },
    )

    assert response.status_code == 200
    user = response.json()["user"]
    assert user["programme"] == "Computer Science"
    assert user["faculty"] == "Faculty of Science and Technology"
    assert user["department"] == "Computing and Information Technology"
    assert user["account_active"] is True
    assert user["account_revoked"] is False


def test_registration_requires_programme_and_faculty_or_department() -> None:
    client = TestClient(app)
    base_payload = {
        "email": "missing.qualification@my.uwi.edu",
        "password": "qualification-password",
        "display_name": "Missing Qualification",
    }

    missing_programme = client.post(
        "/api/auth/register",
        json={**base_payload, "faculty": "FST"},
    )
    missing_organization = client.post(
        "/api/auth/register",
        json={**base_payload, "programme": "Computer Science"},
    )

    assert missing_programme.status_code == 422
    assert missing_organization.status_code == 400
    assert missing_organization.json()["detail"] == "faculty or department is required"


def test_alumni_eligibility_uses_verification_and_account_status() -> None:
    base = {
        "role": "alumni",
        "email_verified": 1,
        "account_active": True,
        "account_revoked": False,
    }

    assert is_eligible_alumni(base) is True
    assert is_eligible_alumni({**base, "email_verified": 0}) is False
    assert is_eligible_alumni({**base, "account_active": False}) is False
    assert is_eligible_alumni({**base, "account_revoked": True}) is False
    assert is_eligible_alumni({**base, "role": "admin"}) is False


def test_admin_can_revoke_and_reactivate_alumni_access() -> None:
    alumni_client = TestClient(app)
    admin_client = TestClient(app)
    alumni = login(
        alumni_client, "alumni.verified@my.uwi.edu", "alumni-dev"
    )
    login(admin_client, "admin@uwi.edu", "admin-dev")

    revoked = admin_client.post(f"/api/admin/users/{alumni['id']}/revoke")

    assert revoked.status_code == 200
    assert revoked.json()["user"]["account_active"] is False
    assert revoked.json()["user"]["account_revoked"] is True
    assert alumni_client.get("/app").status_code == 401

    activated = admin_client.post(f"/api/admin/users/{alumni['id']}/activate")

    assert activated.status_code == 200
    assert activated.json()["user"]["account_active"] is True
    assert activated.json()["user"]["account_revoked"] is False
    assert alumni_client.get("/app").status_code == 200


def test_non_admin_cannot_change_account_status() -> None:
    client = TestClient(app)
    alumni = login(client, "alumni.verified@my.uwi.edu", "alumni-dev")

    response = client.post(f"/api/admin/users/{alumni['id']}/revoke")

    assert response.status_code == 403


def test_user_account_migration_preserves_legacy_records(monkeypatch, tmp_path) -> None:
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-users.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                'CREATE TABLE "user" ('
                "id INTEGER PRIMARY KEY, username VARCHAR NOT NULL, "
                "email VARCHAR NOT NULL, password VARCHAR NOT NULL, "
                "role VARCHAR NOT NULL, email_verified INTEGER NOT NULL, "
                "display_name VARCHAR NOT NULL)"
            )
        )
        connection.execute(
            text(
                'INSERT INTO "user" '
                "(id, username, email, password, role, email_verified, display_name) "
                "VALUES (1, 'legacy', 'legacy@uwi.edu', 'hash', 'alumni', 1, "
                "'Legacy Alumni')"
            )
        )
    monkeypatch.setattr(database, "engine", legacy_engine)

    database._ensure_user_account_columns()
    database._ensure_user_account_columns()

    columns = {column["name"] for column in inspect(legacy_engine).get_columns("user")}
    with Session(legacy_engine) as session:
        legacy_user = session.exec(select(User).where(User.id == 1)).one()

    assert {"programme", "faculty", "department"}.issubset(columns)
    assert {"account_active", "account_revoked"}.issubset(columns)
    assert legacy_user.display_name == "Legacy Alumni"
    assert legacy_user.programme is None
    assert legacy_user.account_active is True
    assert legacy_user.account_revoked is False
