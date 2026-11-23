from app.repositories.user import UserRepository
from app.utilities.security import encrypt_password, verify_password, create_access_token
from app.schemas.user import RegularUserCreate
from typing import Optional
from app.services.accounts import is_uwi_email, normalize_email

class AuthService:
    def __init__(self, user_repo: UserRepository):
        self.user_repo = user_repo

    def authenticate_user(self, username: str, password: str) -> Optional[str]:
        user = self.user_repo.get_by_username(username)
        if not user or not verify_password(plaintext_password=password, encrypted_password=user.password):
            return None
        access_token = create_access_token(data={"sub": f"{user.id}", "role": user.role})
        return access_token

    def register_user(self, username: str, email: str, password: str):
        normalized_email = normalize_email(email)
        if not is_uwi_email(normalized_email):
            raise ValueError("email must use an approved UWI domain")

        if self.user_repo.get_by_email(normalized_email):
            raise ValueError("email already registered")

        new_user = RegularUserCreate(
            username=username, 
            email=normalized_email,
            password=encrypt_password(password)
        )
        return self.user_repo.create(new_user)
