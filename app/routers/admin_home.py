from fastapi import Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.dependencies.auth import AdminDep
from app.services import boards, campaigns
from app.services.board_auth import get_user
from app.utilities.flash import flash

from . import router, templates


def _author_names(listings):
    names = {}
    for listing in listings:
        author_id = listing["author_id"]
        if author_id not in names:
            author = get_user(author_id)
            names[author_id] = (
                (author.get("display_name") or author.get("email"))
                if author
                else f"Author #{author_id}"
            )
    return names


def _campaign_counts(rows):
    return {
        "draft": sum(1 for row in rows if row["status"] == "draft"),
        "scheduled": sum(1 for row in rows if row["status"] == "scheduled"),
        "sent": sum(1 for row in rows if row["status"] == "sent"),
    }


def _dashboard_response(request, user):
    pending = boards.list_pending()
    deletion_result = boards.list_deletion_requests(user.id)
    deletion_requests = deletion_result["listings"] if deletion_result.get("ok") else []
    campaign_rows = campaigns.list_campaigns()

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "user": user,
            "pending_listings": pending,
            "deletion_requests": deletion_requests,
            "campaigns": campaign_rows,
            "authors": _author_names(pending + deletion_requests),
            "pending_count": len(pending),
            "deletion_count": len(deletion_requests),
            "published_count": len(boards.browse_published()),
            "campaign_counts": _campaign_counts(campaign_rows),
        },
    )


def _redirect(request):
    return RedirectResponse(
        url=request.url_for("admin_home_view"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_home_view(request: Request, user: AdminDep):
    return _dashboard_response(request, user)


@router.post("/admin/listings/{listing_id}/publish")
async def admin_publish_listing(request: Request, listing_id: int, user: AdminDep):
    result = boards.publish_listing(user.id, listing_id)
    if result.get("ok"):
        flash(request, f"Listing #{listing_id} approved and published.", "success")
    else:
        flash(
            request,
            result.get("error", "The listing could not be approved."),
            "danger",
        )
    return _redirect(request)


@router.post("/admin/listings/{listing_id}/reject")
async def admin_reject_listing(
    request: Request,
    listing_id: int,
    user: AdminDep,
    rejection_reason: str | None = Form(default=None),
):
    result = boards.reject_listing(
        user.id, listing_id, rejection_reason=rejection_reason
    )
    if result.get("ok"):
        flash(request, f"Listing #{listing_id} rejected.", "success")
    else:
        flash(
            request,
            result.get("error", "The listing could not be rejected."),
            "danger",
        )
    return _redirect(request)


@router.post("/admin/listings/{listing_id}/deletion-request/approve")
async def admin_approve_deletion(request: Request, listing_id: int, user: AdminDep):
    result = boards.approve_deletion(user.id, listing_id)
    if result.get("ok"):
        flash(request, f"Listing #{listing_id} permanently deleted.", "success")
    else:
        flash(
            request,
            result.get("error", "The deletion could not be approved."),
            "danger",
        )
    return _redirect(request)


@router.post("/admin/listings/{listing_id}/deletion-request/deny")
async def admin_deny_deletion(request: Request, listing_id: int, user: AdminDep):
    result = boards.deny_deletion(user.id, listing_id)
    if result.get("ok"):
        flash(request, f"Deletion request for listing #{listing_id} denied.", "success")
    else:
        flash(
            request,
            result.get("error", "The request could not be denied."),
            "danger",
        )
    return _redirect(request)


@router.post("/admin/campaigns")
async def admin_create_campaign(
    request: Request,
    user: AdminDep,
    subject: str = Form(default=""),
    body: str = Form(default=""),
):
    if not subject.strip() or not body.strip():
        flash(request, "Campaign title and message are required.", "danger")
        return _redirect(request)

    result = campaigns.create_draft(user.id, subject=subject, body=body)
    if result.get("ok"):
        flash(request, "Campaign draft created.", "success")
    else:
        flash(
            request,
            result.get("error", "The campaign could not be created."),
            "danger",
        )
    return _redirect(request)


@router.post("/admin/campaigns/{campaign_id}/send")
async def admin_send_campaign(request: Request, campaign_id: int, user: AdminDep):
    result = campaigns.send_campaign(user.id, campaign_id)
    if result.get("ok"):
        flash(
            request,
            "Campaign sent. This publishes an in-app notification; no email is delivered.",
            "success",
        )
    else:
        flash(
            request,
            result.get("error", "The campaign could not be sent."),
            "danger",
        )
    return _redirect(request)


@router.post("/admin/campaigns/{campaign_id}/schedule")
async def admin_schedule_campaign(
    request: Request,
    campaign_id: int,
    user: AdminDep,
    scheduled_at: str = Form(default=""),
):
    result = campaigns.schedule_campaign(
        user.id, campaign_id, scheduled_at=scheduled_at
    )
    if result.get("ok"):
        scheduled = result["campaign"]["scheduled_at"]
        flash(request, f"Campaign scheduled for {scheduled} (UTC).", "success")
    else:
        flash(
            request,
            result.get("error", "The campaign could not be scheduled."),
            "danger",
        )
    return _redirect(request)
