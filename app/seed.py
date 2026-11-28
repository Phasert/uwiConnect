"""Seed demo users and sample listings for the uwi-connect scenario."""

from __future__ import annotations

import importlib
from contextlib import suppress

from sqlmodel import Session, select

from app.database import drop_all, engine, ensure_db_and_tables
from app.models.listing import Listing
from app.models.user import User
from app.services import notifications
from app.services.qualification_options import seed_qualification_options
from app.utilities.security import encrypt_password

USERS = [
    ("admin@uwi.edu", "admin-dev", "admin", 1, "Alumni Admin"),
    ("alumni.verified@my.uwi.edu", "alumni-dev", "alumni", 1, "Verified Alum"),
    ("alumni.pending@gmail.com", "alumni-dev", "alumni", 0, "Unverified Alum"),
    ("alumni.fakeuwi@example.com", "alumni-dev", "alumni", 0, "Substring Trap"),
]
DEMO_ALUMNI_EMAIL = "alumni.verified@my.uwi.edu"
DEMO_QUALIFICATION = {
    "programme": "BSc Computer Science with a Minor in Finance",
    "faculty": "Science and Technology",
    "department": "Computing and Information Technology",
}


def seed(*, reset: bool = False) -> None:
    importlib.import_module("app.models")

    if reset:
        with suppress(Exception):
            drop_all()
        notifications.clear_observers()
    ensure_db_and_tables()

    with Session(engine) as session:
        seed_qualification_options(session)
        existing = session.exec(select(User)).first()
        if existing and not reset:
            alum = session.exec(select(User).where(User.email == DEMO_ALUMNI_EMAIL)).first()
            if alum is not None and not any(getattr(alum, field) for field in DEMO_QUALIFICATION):
                for field, value in DEMO_QUALIFICATION.items():
                    setattr(alum, field, value)
                session.add(alum)
                session.commit()
            return

        for email, password, role, verified, name in USERS:
            session.add(
                User(
                    username=email,
                    email=email,
                    password=encrypt_password(password),
                    role=role,
                    email_verified=verified,
                    display_name=name,
                    **(DEMO_QUALIFICATION if email == DEMO_ALUMNI_EMAIL else {}),
                )
            )
        session.commit()

        admin = session.exec(select(User).where(User.email == "admin@uwi.edu")).one()
        alum = session.exec(
            select(User).where(User.email == DEMO_ALUMNI_EMAIL)
        ).one()

        session.add_all(
            [
                Listing(
                    author_id=admin.id,
                    kind="job",
                    title="Seed Software Role",
                    body="Published seed job.",
                    status="published",
                    created_at="2099-01-01T12:00:00+00:00",
                ),
                Listing(
                    author_id=alum.id,
                    kind="job",
                    title="Pending Alumni Job",
                    body="Awaiting admin publish.",
                    status="pending",
                    created_at="2099-01-02T12:00:00+00:00",
                ),
            ]
        )
        session.commit()


if __name__ == "__main__":
    seed(reset=True)
    print("Seeded uwi-connect scenario data")
