"""Admin-only staff account management pages and actions."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services.authorization import get_admin_page_user
from app.services.session_store import require_valid_csrf
from app.services.staff_management import (
    StaffManagementError,
    change_staff_role,
    create_managed_staff,
    list_staff_accounts,
    reset_staff_password,
    set_staff_active,
)
from app.services.staff_accounts import STAFF_ROLES
from app.templating import templates


router = APIRouter(prefix="/admin/staff", tags=["staff management"])
_SUCCESS_MESSAGES = {
    "created": "Staff account created.",
    "role": "Staff role updated. The account must sign in again.",
    "access": "Staff account access updated.",
    "password": "Password reset. Existing sessions were revoked.",
}


def _private(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _page(
    request: Request,
    db: Session,
    staff: User,
    *,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request,
        name="admin/staff_management.html",
        context={
            "page_title": "Staff management | YatraNest",
            "meta_description": "Manage YatraNest staff accounts and access.",
            "canonical_url": f"{str(request.base_url).rstrip('/')}/admin/staff",
            "robots": "noindex, nofollow",
            "staff": staff,
            "csrf_token": request.state.staff_session.csrf_token,
            "accounts": list_staff_accounts(db),
            "roles": sorted(STAFF_ROLES),
            "errors": errors or [],
            "success": _SUCCESS_MESSAGES.get(request.query_params.get("success", "")),
        },
        status_code=status_code,
    )
    return _private(response)


def _error_page(request: Request, db: Session, staff: User, error: Exception) -> HTMLResponse:
    db.rollback()
    if isinstance(error, StaffManagementError):
        return _page(request, db, staff, errors=[str(error)], status_code=error.status_code)
    return _page(request, db, staff, errors=["The staff account change could not be saved. Check the email and try again."], status_code=409)


@router.get("", response_class=HTMLResponse)
def staff_management_page(
    request: Request,
    staff: Annotated[User, Depends(get_admin_page_user)],
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _page(request, db, staff)


@router.post("")
def staff_create(
    request: Request,
    staff: Annotated[User, Depends(get_admin_page_user)],
    db: Session = Depends(get_db),
    csrf_token: Annotated[str, Form(max_length=128)] = "",
    name: Annotated[str, Form(max_length=160)] = "",
    email: Annotated[str, Form(max_length=320)] = "",
    role: Annotated[str, Form(max_length=20)] = "",
    password: Annotated[str, Form(max_length=1024)] = "",
    password_confirm: Annotated[str, Form(max_length=1024)] = "",
    confirm_action: Annotated[str, Form()] = "",
) -> Response:
    require_valid_csrf(request.state.staff_session, csrf_token)
    if confirm_action != "yes":
        return _page(request, db, staff, errors=["Confirm that you want to create this staff account."], status_code=422)
    if password != password_confirm:
        return _page(request, db, staff, errors=["The passwords do not match."], status_code=422)
    try:
        create_managed_staff(db, actor_id=staff.id, name=name, email=email, role=role, password=password)
        db.commit()
    except (StaffManagementError, IntegrityError) as exc:
        return _error_page(request, db, staff, exc)
    response = RedirectResponse("/admin/staff?success=created", status_code=303)
    return _private(response)


@router.post("/{staff_id:uuid}/role")
def staff_change_role(
    staff_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_admin_page_user)],
    db: Session = Depends(get_db),
    csrf_token: Annotated[str, Form(max_length=128)] = "",
    role: Annotated[str, Form(max_length=20)] = "",
    confirm_action: Annotated[str, Form()] = "",
) -> Response:
    require_valid_csrf(request.state.staff_session, csrf_token)
    if confirm_action != "yes":
        return _page(request, db, staff, errors=["Confirm that you want to change this staff member’s role."], status_code=422)
    try:
        change_staff_role(db, actor_id=staff.id, staff_id=staff_id, role=role)
        db.commit()
    except (StaffManagementError, IntegrityError) as exc:
        return _error_page(request, db, staff, exc)
    response = RedirectResponse("/admin/staff?success=role", status_code=303)
    return _private(response)


@router.post("/{staff_id:uuid}/active")
def staff_set_active(
    staff_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_admin_page_user)],
    db: Session = Depends(get_db),
    csrf_token: Annotated[str, Form(max_length=128)] = "",
    active: Annotated[str, Form(max_length=5)] = "",
    confirm_action: Annotated[str, Form()] = "",
) -> Response:
    require_valid_csrf(request.state.staff_session, csrf_token)
    if confirm_action != "yes" or active not in {"true", "false"}:
        return _page(request, db, staff, errors=["Confirm the valid activation or deactivation action."], status_code=422)
    try:
        set_staff_active(db, actor_id=staff.id, staff_id=staff_id, is_active=active == "true")
        db.commit()
    except (StaffManagementError, IntegrityError) as exc:
        return _error_page(request, db, staff, exc)
    response = RedirectResponse("/admin/staff?success=access", status_code=303)
    return _private(response)


@router.post("/{staff_id:uuid}/password")
def staff_reset_password(
    staff_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_admin_page_user)],
    db: Session = Depends(get_db),
    csrf_token: Annotated[str, Form(max_length=128)] = "",
    new_password: Annotated[str, Form(max_length=1024)] = "",
    password_confirm: Annotated[str, Form(max_length=1024)] = "",
    confirm_action: Annotated[str, Form()] = "",
) -> Response:
    require_valid_csrf(request.state.staff_session, csrf_token)
    if confirm_action != "yes":
        return _page(request, db, staff, errors=["Confirm that you want to reset this password."], status_code=422)
    if new_password != password_confirm:
        return _page(request, db, staff, errors=["The passwords do not match."], status_code=422)
    try:
        reset_staff_password(db, actor_id=staff.id, staff_id=staff_id, new_password=new_password)
        db.commit()
    except (StaffManagementError, IntegrityError) as exc:
        return _error_page(request, db, staff, exc)
    response = RedirectResponse("/admin/staff?success=password", status_code=303)
    return _private(response)
