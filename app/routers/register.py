from fastapi import BackgroundTasks, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.dependencies import SessionDep
from app.repositories.user import UserRepository
from app.services.accounts import send_verification_email
from app.services.auth_service import AuthService
from app.utilities.flash import flash

from . import router, templates


# View route (loads the page)
@router.get("/register", response_class=HTMLResponse)
async def register_view(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="register.html",
    )


# Action route (performs an action)
@router.post("/register", response_class=HTMLResponse, status_code=status.HTTP_201_CREATED)
def signup_user(
    request: Request,
    background_tasks: BackgroundTasks,
    db: SessionDep,
    username: str = Form(),
    email: str = Form(),
    password: str = Form(),
    programme: str = Form(),
    faculty: str | None = Form(default=None),
    department: str | None = Form(default=None),
):
    user_repo = UserRepository(db)
    auth_service = AuthService(user_repo)
    try:
        user = auth_service.register_user(
            username, email, password, programme, faculty, department
        )
        if user.id is not None:
            background_tasks.add_task(send_verification_email, user.id)
        flash(
            request,
            "Registration completed! Check your email to verify your account.",
        )
        return RedirectResponse(
            url=request.url_for("login_view"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except ValueError as exc:
        flash(request, str(exc), "danger")
        return RedirectResponse(
            url=request.url_for("register_view"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
