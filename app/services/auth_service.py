
from app.repositories.user import UserRepository
from app.schemas.user import RegularUserCreate
from app.services.accounts import is_approved_registration_email, normalize_email
from app.services.alumni_eligibility import clean_qualification, is_account_accessible
from app.utilities.security import (
    create_access_token,
    encrypt_password,
    verify_password,
)


class AuthService:
    def __init__(self, user_repo: UserRepository):
        self.user_repo = user_repo

    def authenticate_user(self, username: str, password: str) -> str | None:
        user = self.user_repo.get_by_username(username)
        if (
            not user
            or not is_account_accessible(user)
            or not verify_password(
                plaintext_password=password, encrypted_password=user.password
            )
            or not user.email_verified
        ):
            return None
        access_token = create_access_token(data={"sub": f"{user.id}", "role": user.role})
        return access_token

    def register_user(
        self,
        username: str,
        email: str,
        password: str,
        programme: str,
        faculty: str | None = None,
        department: str | None = None,
    ):
        normalized_email = normalize_email(email)
        if not is_approved_registration_email(normalized_email):
            raise ValueError("email must use an approved UWI domain")

        if self.user_repo.get_by_email(normalized_email):
            raise ValueError("email already registered")

        qualification, error = clean_qualification(programme, faculty, department)
        if error:
            raise ValueError(error)

        new_user = RegularUserCreate(
            username=username, 
            email=normalized_email,
            password=encrypt_password(password),
            role="alumni",
            **qualification,
        )
        return self.user_repo.create(new_user)
