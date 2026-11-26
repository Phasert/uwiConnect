import jwt
import pytest

from app.config import get_settings
from app.repositories.user import UserRepository
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.utilities.security import verify_password


def test_save_password(test_session):
    repository = UserRepository(test_session)
    service = AuthService(repository)

    created = service.register_user(
        "alice", "Alice@UWI.EDU", "secret", "Computer Science", "FST"
    )
    saved = repository.get_by_username("alice")

    assert saved is not None
    assert created.id == saved.id
    assert saved.username == "alice"
    assert saved.email == "alice@uwi.edu"
    assert saved.role == "alumni"
    assert saved.password != "secret"
    assert verify_password("secret", saved.password)


def test_login(test_session):
    repository = UserRepository(test_session)
    service = AuthService(repository)
    user = service.register_user(
        "alice", "Alice@UWI.EDU", "secret", "Computer Science", "FST"
    )
    user.email_verified = 1
    test_session.commit()

    token = service.authenticate_user("alice", "secret")
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
    )

    assert payload["sub"] == str(repository.get_by_username("alice").id)
    assert payload["role"] == "alumni"


def test_register_user(test_session):
    repository = UserRepository(test_session)
    auth_service = AuthService(repository)
    auth_service.register_user(
        "alice", "Alice@MY.UWI.EDU", "secret", "Computer Science", "FST"
    )

    users = UserService(repository).get_all_users()

    assert len(users) == 1
    assert users[0].username == "alice"
    assert users[0].email == "alice@my.uwi.edu"


@pytest.mark.parametrize("email", ["alice@gmail.com", "alice@uwi.edu.attacker.com"])
def test_ssr_registration_rejects_invalid_email(test_session, email):
    service = AuthService(UserRepository(test_session))

    with pytest.raises(ValueError, match="approved UWI domain"):
        service.register_user("alice", email, "secret", "Computer Science", "FST")


def test_ssr_registration_rejects_duplicate_case_insensitively(test_session):
    repository = UserRepository(test_session)
    service = AuthService(repository)

    service.register_user(
        "alice", "Alice@UWI.EDU", "secret", "Computer Science", "FST"
    )

    with pytest.raises(ValueError, match="email already registered"):
        service.register_user(
            "other", "ALICE@uwi.edu", "other-secret", "Computer Science", "FST"
        )


def test_ssr_authenticate_accepts_username_unchanged(test_session):
    repository = UserRepository(test_session)
    service = AuthService(repository)
    user = service.register_user(
        "alice", "Alice@MY.UWI.EDU", "secret", "Computer Science", "FST"
    )
    user.email_verified = 1
    test_session.commit()

    token = service.authenticate_user("alice", "secret")

    assert token is not None
