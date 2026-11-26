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


def _deletion_requests(admin_id):
    result = boards.list_deletion_requests(admin_id)
    return result["listings"] if result.get("ok") else []


def _revision_requests(admin_id):
    result = boards.list_revision_requests(admin_id)
    return result["revision_requests"] if result.get("ok") else []


def _campaign_counts(rows):
    return {
        "draft": sum(1 for row in rows if row["status"] == "draft"),
        "scheduled": sum(1 for row in rows if row["status"] == "scheduled"),
        "sent": sum(1 for row in rows if row["status"] == "sent"),
    }


def _render(request, user, template_name, context=None):
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={"user": user, **(context or {})},
    )


def _redirect(request, route_name):
    return RedirectResponse(
        url=request.url_for(route_name),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_home_view(request: Request, user: AdminDep):
    pending = boards.list_pending()
    deletion_requests = _deletion_requests(user.id)
    revision_requests = _revision_requests(user.id)
    campaign_rows = campaigns.list_campaigns()

    return _render(
        request,
        user,
        "admin.html",
        {
            "pending_count": len(pending),
            "deletion_count": len(deletion_requests),
            "revision_count": len(revision_requests),
            "published_count": len(boards.browse_published()),
            "campaign_counts": _campaign_counts(campaign_rows),
        },
    )


@router.get("/admin/moderation", response_class=HTMLResponse)
async def admin_moderation_view(request: Request, user: AdminDep):
    pending = boards.list_pending()

    return _render(
        request,
        user,
        "admin/moderation.html",
        {
            "pending_listings": pending,
            "authors": _author_names(pending),
        },
    )


@router.get("/admin/revisions", response_class=HTMLResponse)
async def admin_revisions_view(request: Request, user: AdminDep):
    revision_requests = _revision_requests(user.id)

    return _render(
        request,
        user,
        "admin/revisions.html",
        {
            "revision_requests": revision_requests,
            "authors": _author_names([item["original"] for item in revision_requests]),
        },
    )


@router.get("/admin/deletions", response_class=HTMLResponse)
async def admin_deletions_view(request: Request, user: AdminDep):
    deletion_requests = _deletion_requests(user.id)

    return _render(
        request,
        user,
        "admin/deletions.html",
        {
            "deletion_requests": deletion_requests,
            "authors": _author_names(deletion_requests),
        },
    )


@router.get("/admin/campaigns", response_class=HTMLResponse)
async def admin_campaigns_view(request: Request, user: AdminDep):
    return _render(
        request,
        user,
        "admin/campaigns.html",
        {
            "campaigns": campaigns.list_campaigns(),
            "audience_options": campaigns.audience_options(),
        },
    )


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
    return _redirect(request, "admin_moderation_view")


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
    return _redirect(request, "admin_moderation_view")


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
    return _redirect(request, "admin_deletions_view")


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
    return _redirect(request, "admin_deletions_view")


@router.post("/admin/listings/{listing_id}/revision-request/approve")
async def admin_approve_revision(request: Request, listing_id: int, user: AdminDep):
    result = boards.approve_revision(user.id, listing_id)
    if result.get("ok"):
        flash(request, f"Listing #{listing_id} revision approved.", "success")
    else:
        flash(
            request,
            result.get("error", "The revision could not be approved."),
            "danger",
        )
    return _redirect(request, "admin_revisions_view")


@router.post("/admin/listings/{listing_id}/revision-request/deny")
async def admin_deny_revision(request: Request, listing_id: int, user: AdminDep):
    result = boards.deny_revision(user.id, listing_id)
    if result.get("ok"):
        flash(request, f"Revision request for listing #{listing_id} denied.", "success")
    else:
        flash(
            request,
            result.get("error", "The revision request could not be denied."),
            "danger",
        )
    return _redirect(request, "admin_revisions_view")


@router.post("/admin/campaigns")
async def admin_create_campaign(
    request: Request,
    user: AdminDep,
    subject: str = Form(default=""),
    body: str = Form(default=""),
    audience_type: str = Form(default="all"),
    faculty: str = Form(default=""),
    department: str = Form(default=""),
):
    if not subject.strip() or not body.strip():
        flash(request, "Campaign title and message are required.", "danger")
        return _redirect(request, "admin_campaigns_view")

    if (
        (audience_type == "all" and (faculty.strip() or department.strip()))
        or (audience_type == "faculty" and department.strip())
        or (audience_type == "department" and faculty.strip())
    ):
        flash(request, "Choose only the value for the selected audience.", "danger")
        return _redirect(request, "admin_campaigns_view")

    audience_value = {"faculty": faculty, "department": department}.get(audience_type)
    result = campaigns.create_draft(
        user.id,
        subject=subject,
        body=body,
        audience_type=audience_type,
        audience_value=audience_value,
    )
    if result.get("ok"):
        flash(request, "Campaign draft created.", "success")
    else:
        flash(
            request,
            result.get("error", "The campaign could not be created."),
            "danger",
        )
    return _redirect(request, "admin_campaigns_view")


@router.post("/admin/campaigns/{campaign_id}/send")
async def admin_send_campaign(request: Request, campaign_id: int, user: AdminDep):
    result = campaigns.send_campaign(user.id, campaign_id)
    if result.get("ok"):
        flash(
            request,
            "Campaign notification processing completed. Eligible alumni receive website notifications; enabled email delivery is best-effort and does not confirm inbox receipt.",
            "success",
        )
    else:
        flash(
            request,
            result.get("error", "The campaign could not be sent."),
            "danger",
        )
    return _redirect(request, "admin_campaigns_view")


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
    return _redirect(request, "admin_campaigns_view")
