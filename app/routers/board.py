"""UWI Connect scenario JSON API (OpenAPI /docs)."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, NamedTuple

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from app.database import engine
from app.dependencies import AdminDep, AuthDep, get_csrf_protection
from app.dependencies.auth import get_current_user
from app.services import accounts, boards, campaigns, notification_stream, notifications
from app.services import board_auth as auth
from app.services.verification_resend import RESEND_MESSAGE, check_resend_rate_limit

router = APIRouter(tags=["uwi-connect"])


class LoginBody(BaseModel):
    email: str
    password: str


class RegisterBody(BaseModel):
    email: str
    password: str
    display_name: str = "Alumni"
    programme: str
    faculty: str | None = None
    department: str | None = None


class VerifyEmailBody(BaseModel):
    token: str


class ResendVerificationBody(BaseModel):
    email: str = Field(max_length=320)


class ListingBody(BaseModel):
    kind: str = Field(description="job | event")
    title: str
    body: str
    company_name: str | None = None
    location: str | None = None
    application_instructions: str | None = None
    application_deadline: str | None = None
    faculty: str | None = None
    department: str | None = None
    event_date: str | None = None
    event_time: str | None = None
    category: str | None = None
    registration_information: str | None = None


class ListingEditBody(ListingBody):
    model_config = ConfigDict(extra="forbid")


class RejectionBody(BaseModel):
    rejection_reason: str | None = None


class CampaignBody(BaseModel):
    subject: str
    body: str


class ScheduleCampaignBody(BaseModel):
    scheduled_at: str


def _actor_id(x_user_id: str | None) -> int:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="X-User-Id header required")
    try:
        return int(x_user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="X-User-Id must be int") from exc


_MAX_SSE_CURSOR = 2**63 - 1
_MAX_SSE_CURSOR_DIGITS = len(str(_MAX_SSE_CURSOR))
_INVALID_CURSOR_DETAIL = "Last-Event-ID must be a positive integer"


def _parse_last_event_id(value: str | None) -> int:
    if value is None:
        return 0
    raw = value.strip()
    if (
        not raw
        or len(raw) > _MAX_SSE_CURSOR_DIGITS
        or not raw.isascii()
        or not raw.isdecimal()
    ):
        raise HTTPException(status_code=400, detail=_INVALID_CURSOR_DETAIL)
    try:
        cursor = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_INVALID_CURSOR_DETAIL) from exc
    if cursor <= 0 or cursor > _MAX_SSE_CURSOR:
        raise HTTPException(status_code=400, detail=_INVALID_CURSOR_DETAIL)
    return cursor


async def _validate_stream_cursor(*, user_id: int, value: str | None) -> int:
    cursor = _parse_last_event_id(value)
    if cursor:
        high_water = await asyncio.to_thread(
            notification_stream.user_notification_high_water_mark,
            user_id=user_id,
        )
        if cursor > high_water:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Last-Event-ID is ahead of notification stream; "
                    "resynchronise from /api/notifications"
                ),
            )
    return cursor


class _StreamIdentity(NamedTuple):
    user_id: int
    expires_at: float | None


async def _stream_identity(request: Request) -> _StreamIdentity:
    """Authenticate once, then close the dependency's database session."""
    token = request.cookies.get("access_token")
    with Session(engine) as session:
        user = await get_current_user(request, session)
        user_id = user.id
    if user_id is None:
        raise HTTPException(status_code=401, detail="Could not validate credentials")
    return _StreamIdentity(user_id, notification_stream.token_expiry_timestamp(token))


StreamIdentityDep = Annotated[_StreamIdentity, Depends(_stream_identity)]


@router.post("/auth/login")
def login(body: LoginBody) -> dict[str, Any]:
    user = auth.login(body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="invalid credentials")
    return {"user": user}


@router.post("/auth/register")
def register(
    body: RegisterBody,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    result = accounts.register(
        body.email,
        body.password,
        body.display_name,
        body.programme,
        body.faculty,
        body.department,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    background_tasks.add_task(
        accounts.send_verification_email,
        result["user"]["id"],
    )
    return result


@router.get("/auth/verify-email")
def verify_email_by_link(token: str) -> dict[str, Any]:
    result = accounts.verify_email_token(token)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/auth/resend-verification")
def resend_verification(
    body: ResendVerificationBody,
    request: Request,
    background_tasks: BackgroundTasks,
    _csrf: None = Depends(get_csrf_protection),
) -> dict[str, str]:
    check_resend_rate_limit(request)
    background_tasks.add_task(accounts.resend_verification_email, body.email)
    return {"message": RESEND_MESSAGE}


@router.post("/auth/verify-email")
def verify_email(body: VerifyEmailBody) -> dict[str, Any]:
    result = accounts.verify_email_token(body.token)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.get("/boards")
def browse(
    kind: str | None = None,
    q: str | None = None,
    category: str | None = None,
    faculty: str | None = None,
    department: str | None = None,
) -> dict[str, Any]:
    return {
        "listings": boards.browse_published(
            kind=kind,
            q=q,
            category=category,
            faculty=faculty,
            department=department,
        )
    }


@router.post("/boards/listings")
def submit(
    body: ListingBody,
    user: AuthDep,
    _csrf: None = Depends(get_csrf_protection),
) -> dict[str, Any]:
    result = boards.submit_listing(user.id, **body.model_dump())
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.put("/boards/listings/{listing_id}")
def edit(
    listing_id: int,
    body: ListingEditBody,
    user: AuthDep,
    _csrf: None = Depends(get_csrf_protection),
) -> dict[str, Any]:
    result = boards.edit_listing(
        user.id, listing_id, body.model_dump(exclude_unset=True)
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/boards/listings/{listing_id}/revision-request")
def request_revision(
    listing_id: int,
    body: ListingEditBody,
    user: AuthDep,
    _csrf: None = Depends(get_csrf_protection),
) -> dict[str, Any]:
    result = boards.request_revision(
        user.id, listing_id, body.model_dump(exclude_unset=True)
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/boards/listings/{listing_id}/deletion-request")
def request_deletion(
    listing_id: int,
    user: AuthDep,
    _csrf: None = Depends(get_csrf_protection),
) -> dict[str, Any]:
    result = boards.request_deletion(user.id, listing_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.get("/admin/listings/pending")
def pending(admin: AdminDep) -> dict[str, Any]:
    return {"listings": boards.list_pending()}


@router.get("/admin/listings/deletion-requests")
def deletion_requests(admin: AdminDep) -> dict[str, Any]:
    result = boards.list_deletion_requests(admin.id)
    if not result.get("ok"):
        raise HTTPException(status_code=403, detail=result.get("error"))
    return {"listings": result["listings"]}


@router.get("/admin/listings/revision-requests")
def revision_requests(admin: AdminDep) -> dict[str, Any]:
    result = boards.list_revision_requests(admin.id)
    if not result.get("ok"):
        raise HTTPException(status_code=403, detail=result.get("error"))
    return {"revision_requests": result["revision_requests"]}


@router.post("/admin/listings/{listing_id}/revision-request/approve")
def approve_revision(
    listing_id: int,
    admin: AdminDep,
    _csrf: None = Depends(get_csrf_protection),
) -> dict[str, Any]:
    result = boards.approve_revision(admin.id, listing_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/admin/listings/{listing_id}/revision-request/deny")
def deny_revision(
    listing_id: int,
    admin: AdminDep,
    _csrf: None = Depends(get_csrf_protection),
) -> dict[str, Any]:
    result = boards.deny_revision(admin.id, listing_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/admin/listings/{listing_id}/deletion-request/approve")
def approve_deletion(
    listing_id: int,
    admin: AdminDep,
    _csrf: None = Depends(get_csrf_protection),
) -> dict[str, Any]:
    result = boards.approve_deletion(admin.id, listing_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/admin/listings/{listing_id}/deletion-request/deny")
def deny_deletion(
    listing_id: int,
    admin: AdminDep,
    _csrf: None = Depends(get_csrf_protection),
) -> dict[str, Any]:
    result = boards.deny_deletion(admin.id, listing_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/admin/listings/{listing_id}/publish")
def publish(
    listing_id: int,
    admin: AdminDep,
    _csrf: None = Depends(get_csrf_protection),
) -> dict[str, Any]:
    result = boards.publish_listing(admin.id, listing_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/admin/listings/{listing_id}/reject")
def reject(
    listing_id: int,
    admin: AdminDep,
    body: RejectionBody | None = None,
    _csrf: None = Depends(get_csrf_protection),
) -> dict[str, Any]:
    rejection_reason = body.rejection_reason if body else None
    result = boards.reject_listing(
        admin.id, listing_id, rejection_reason=rejection_reason
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/admin/campaigns")
def create_campaign(
    body: CampaignBody, x_user_id: str | None = Header(default=None)
) -> dict[str, Any]:
    result = campaigns.create_draft(
        _actor_id(x_user_id), subject=body.subject, body=body.body
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.get("/admin/campaigns")
def list_campaigns(x_user_id: str | None = Header(default=None)) -> dict[str, Any]:
    actor = auth.get_user(_actor_id(x_user_id))
    if not actor or actor["role"] != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return {"campaigns": campaigns.list_campaigns()}


@router.post("/admin/campaigns/{campaign_id}/send")
def send_campaign(
    campaign_id: int, x_user_id: str | None = Header(default=None)
) -> dict[str, Any]:
    result = campaigns.send_campaign(_actor_id(x_user_id), campaign_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/admin/campaigns/{campaign_id}/schedule")
def schedule_campaign(
    campaign_id: int,
    body: ScheduleCampaignBody,
    x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    result = campaigns.schedule_campaign(
        _actor_id(x_user_id),
        campaign_id,
        scheduled_at=body.scheduled_at,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.get("/notifications")
def my_notifications(user: AuthDep) -> dict[str, Any]:
    return {"notifications": notifications.inbox(user.id)}


@router.get("/notifications/stream")
async def stream_notifications(
    request: Request,
    identity: StreamIdentityDep,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    cursor = await _validate_stream_cursor(
        user_id=identity.user_id, value=last_event_id
    )
    return StreamingResponse(
        notification_stream.iter_notification_events(
            request=request,
            user_id=identity.user_id,
            cursor=cursor,
            expires_at=identity.expires_at,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
