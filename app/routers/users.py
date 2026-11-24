from fastapi import Depends, HTTPException, Request

from app.dependencies import AdminDep, SessionDep, get_csrf_protection
from app.models.user import User
from app.repositories.user import UserRepository
from app.schemas import UserResponse
from app.services.board_auth import public_user
from app.services.user_service import UserService

from . import api_router


# API endpoint for listing users
@api_router.get("/users", response_model=list[UserResponse])
async def list_users(request: Request, db: SessionDep):
    user_repo = UserRepository(db)
    user_service = UserService(user_repo)
    return user_service.get_all_users()


def _set_account_status(db: SessionDep, user_id: int, *, active: bool) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if user.role != "alumni":
        raise HTTPException(status_code=400, detail="alumni account required")
    user.account_active = active
    user.account_revoked = not active
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"ok": True, "user": public_user(user)}


@api_router.post("/admin/users/{user_id}/revoke")
def revoke_alumni(
    user_id: int,
    admin: AdminDep,
    db: SessionDep,
    _csrf: None = Depends(get_csrf_protection),
) -> dict:
    return _set_account_status(db, user_id, active=False)


@api_router.post("/admin/users/{user_id}/activate")
def activate_alumni(
    user_id: int,
    admin: AdminDep,
    db: SessionDep,
    _csrf: None = Depends(get_csrf_protection),
) -> dict:
    return _set_account_status(db, user_id, active=True)
