"""Staff quotation workflow and private-token customer viewing."""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import Enquiry, Package, PackagePrice, Quotation, User
from app.services.authorization import get_staff_page_user
from app.services.enquiry_queries import get_enquiry_for_staff
from app.services.quotation_pdf import render_quotation_pdf
from app.services.quotations import (
    QuotationError,
    approve_quotation,
    create_quotation,
    get_quotation_by_share_token,
    get_quotation_for_staff,
    issue_share_token,
)
from app.services.session_store import require_valid_csrf
from app.templating import format_money, templates


router = APIRouter(tags=["quotations"])


def _private(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _packages_and_prices(db: Session) -> tuple[list[Package], list[dict[str, object]]]:
    packages = list(db.scalars(
        select(Package).options(selectinload(Package.prices))
        .where(Package.status == "published").order_by(Package.title.asc())
    ).unique())
    options: list[dict[str, object]] = []
    for package in packages:
        for price in package.prices:
            options.append({
                "id": str(price.id),
                "package_id": str(package.id),
                "label": (
                    f"{package.package_code} · {package.title} · {price.season_name} · "
                    f"{format_money(price.price_per_person, price.currency)} / person"
                ),
                "price_basis": price.price_basis,
            })
    return packages, options


def _default_package_price_id(enquiry: Enquiry) -> str:
    """Select a verified rate only when exactly one covers the full enquiry period."""
    package = enquiry.package
    start = enquiry.travel_start_date
    end = enquiry.travel_end_date
    if package is None or package.status != "published" or start is None or end is None:
        return ""
    day_count = (end - start).days
    if day_count < 0 or day_count > 366:
        return ""
    matching = [
        price for price in package.prices
        if all(price.applies_on(date.fromordinal(day)) for day in range(start.toordinal(), end.toordinal() + 1))
    ]
    return str(matching[0].id) if len(matching) == 1 else ""


def _form_values(form: dict[str, object] | None = None) -> dict[str, str]:
    form = form or {}
    keys = (
        "package_id", "package_price_id", "travel_start_date", "travel_end_date", "adults",
        "children", "infants", "accommodation", "manual_adult_price", "child_unit_price",
        "infant_unit_price", "manual_currency", "manual_price_basis", "gst_rate_percent", "inclusions", "exclusions", "payment_terms",
    )
    return {key: str(form.get(key, "")) for key in keys}


def _form_page(
    request: Request,
    db: Session,
    staff: User,
    enquiry: Enquiry,
    *,
    values: dict[str, str],
    errors: list[str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    packages, price_options = _packages_and_prices(db)
    response = templates.TemplateResponse(
        request=request,
        name="admin/quotation_form.html",
        context={
            "page_title": "Create quotation | YatraNest",
            "meta_description": "Create a staff-reviewed travel quotation.",
            "canonical_url": f"{str(request.base_url).rstrip('/')}/admin/enquiries/{enquiry.id}/quotations/new",
            "robots": "noindex, nofollow",
            "staff": staff,
            "csrf_token": request.state.staff_session.csrf_token,
            "enquiry": enquiry,
            "packages": packages,
            "price_options": price_options,
            "values": values,
            "errors": errors or [],
        },
        status_code=status_code,
    )
    return _private(response)


@router.get("/admin/enquiries/{enquiry_id:uuid}/quotations/new", response_class=HTMLResponse)
def quotation_new(
    enquiry_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)],
    db: Session = Depends(get_db),
) -> HTMLResponse:
    enquiry = get_enquiry_for_staff(db, enquiry_id, staff)
    if enquiry is None:
        raise HTTPException(status_code=404, detail="Enquiry not found.")
    values = {
        "package_id": str(enquiry.package_id or ""),
        "package_price_id": _default_package_price_id(enquiry),
        "travel_start_date": enquiry.travel_start_date.isoformat() if enquiry.travel_start_date else "",
        "travel_end_date": enquiry.travel_end_date.isoformat() if enquiry.travel_end_date else "",
        "adults": str(enquiry.adults) if enquiry.adults is not None else "",
        "children": str(enquiry.children) if enquiry.children is not None else "",
        "infants": str(enquiry.infants) if enquiry.infants is not None else "",
        "accommodation": enquiry.package.hotel_category if enquiry.package and enquiry.package.hotel_category else "",
        "manual_adult_price": "", "child_unit_price": "", "infant_unit_price": "",
        "manual_currency": "INR", "gst_rate_percent": "", "inclusions": "", "exclusions": "", "payment_terms": "",
        "manual_price_basis": "per_person_twin_sharing",
    }
    return _form_page(request, db, staff, enquiry, values=values)


@router.post("/admin/enquiries/{enquiry_id:uuid}/quotations", response_class=HTMLResponse)
def quotation_create(
    enquiry_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)],
    db: Session = Depends(get_db),
    csrf_token: Annotated[str, Form(max_length=128)] = "",
    package_id: Annotated[str, Form(max_length=36)] = "",
    package_price_id: Annotated[str, Form(max_length=36)] = "",
    travel_start_date: Annotated[str, Form(max_length=10)] = "",
    travel_end_date: Annotated[str, Form(max_length=10)] = "",
    adults: Annotated[str, Form(max_length=3)] = "",
    children: Annotated[str, Form(max_length=3)] = "",
    infants: Annotated[str, Form(max_length=3)] = "",
    accommodation: Annotated[str, Form(max_length=160)] = "",
    manual_adult_price: Annotated[str, Form(max_length=13)] = "",
    child_unit_price: Annotated[str, Form(max_length=13)] = "",
    infant_unit_price: Annotated[str, Form(max_length=13)] = "",
    manual_currency: Annotated[str, Form(max_length=3)] = "INR",
    manual_price_basis: Annotated[str, Form(max_length=32)] = "per_person_twin_sharing",
    gst_rate_percent: Annotated[str, Form(max_length=6)] = "",
    staff_price_confirmed: Annotated[str | None, Form()] = None,
    inclusions: Annotated[str, Form(max_length=6000)] = "",
    exclusions: Annotated[str, Form(max_length=6000)] = "",
    payment_terms: Annotated[str, Form(max_length=4000)] = "",
) -> Response:
    require_valid_csrf(request.state.staff_session, csrf_token)
    enquiry = get_enquiry_for_staff(db, enquiry_id, staff)
    if enquiry is None:
        raise HTTPException(status_code=404, detail="Enquiry not found.")
    form_values = {
        "package_id": package_id, "package_price_id": package_price_id,
        "travel_start_date": travel_start_date, "travel_end_date": travel_end_date,
        "adults": adults, "children": children, "infants": infants,
        "accommodation": accommodation, "manual_adult_price": manual_adult_price,
        "child_unit_price": child_unit_price, "infant_unit_price": infant_unit_price,
        "manual_currency": manual_currency, "gst_rate_percent": gst_rate_percent,
        "manual_price_basis": manual_price_basis,
        "inclusions": inclusions, "exclusions": exclusions, "payment_terms": payment_terms,
    }
    try:
        quote = create_quotation(
            db, enquiry=enquiry, actor=staff,
            package_id=UUID(package_id),
            package_price_id=UUID(package_price_id) if package_price_id else None,
            travel_start_date=date.fromisoformat(travel_start_date),
            travel_end_date=date.fromisoformat(travel_end_date),
            adults=int(adults), children=int(children), infants=int(infants),
            accommodation=accommodation,
            manual_adult_price=manual_adult_price,
            child_unit_price=child_unit_price,
            infant_unit_price=infant_unit_price,
            manual_currency=manual_currency,
            manual_price_basis=manual_price_basis,
            gst_rate_percent=gst_rate_percent,
            staff_price_confirmed=staff_price_confirmed == "yes",
            inclusions=inclusions, exclusions=exclusions, payment_terms=payment_terms,
        )
        db.commit()
    except (ValueError, TypeError, OverflowError) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, QuotationError) else "Check the quotation fields and try again."
        status_code = exc.status_code if isinstance(exc, QuotationError) else 422
        return _form_page(request, db, staff, enquiry, values=form_values, errors=[message], status_code=status_code)
    return RedirectResponse(f"/admin/quotations/{quote.id}", status_code=303)


def _staff_quote_or_404(db: Session, quote_id: UUID, staff: User) -> Quotation:
    quote = get_quotation_for_staff(db, quote_id, staff)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quotation not found.")
    return quote


def _quote_detail_response(request: Request, quote: Quotation, staff: User, *, csrf: str, success: str | None = None) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request,
        name="admin/quotation_detail.html",
        context={
            "page_title": f"Quotation · {quote.package_title_snapshot} | YatraNest",
            "meta_description": "Private quotation workspace.",
            "canonical_url": f"{str(request.base_url).rstrip('/')}/admin/quotations/{quote.id}",
            "robots": "noindex, nofollow", "staff": staff, "quote": quote,
            "csrf_token": csrf, "success": success,
        },
    )
    return _private(response)


@router.get("/admin/quotations/{quote_id:uuid}", response_class=HTMLResponse)
def quotation_detail(
    quote_id: UUID, request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)], db: Session = Depends(get_db),
) -> HTMLResponse:
    quote = _staff_quote_or_404(db, quote_id, staff)
    success = "Quotation approved. This is not a booking confirmation." if request.query_params.get("approved") == "1" else None
    return _quote_detail_response(request, quote, staff, csrf=request.state.staff_session.csrf_token, success=success)


@router.post("/admin/quotations/{quote_id:uuid}/approve")
def quotation_approve(
    quote_id: UUID, request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)], db: Session = Depends(get_db),
    csrf_token: Annotated[str, Form(max_length=128)] = "",
    expected_updated_at: Annotated[str, Form(max_length=64)] = "",
) -> RedirectResponse:
    require_valid_csrf(request.state.staff_session, csrf_token)
    try:
        quote = approve_quotation(db, quote_id=quote_id, actor=staff, expected_updated_at=expected_updated_at)
        db.commit()
    except QuotationError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    return RedirectResponse(f"/admin/quotations/{quote.id}?approved=1", status_code=303)


@router.post("/admin/quotations/{quote_id:uuid}/share-link", response_class=HTMLResponse)
def quotation_share_link(
    quote_id: UUID, request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)], db: Session = Depends(get_db),
    csrf_token: Annotated[str, Form(max_length=128)] = "",
) -> HTMLResponse:
    require_valid_csrf(request.state.staff_session, csrf_token)
    try:
        quote, token = issue_share_token(db, quote_id=quote_id, actor=staff)
        db.commit()
    except QuotationError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    share_url = str(request.base_url).rstrip("/") + f"/quotes/{quote.id}/{token}"
    response = templates.TemplateResponse(
        request=request, name="admin/quotation_share_link.html",
        context={"page_title": "Private quotation link | YatraNest", "meta_description": "One-time quotation sharing link.",
                 "canonical_url": f"{str(request.base_url).rstrip('/')}/admin/quotations/{quote.id}",
                 "robots": "noindex, nofollow", "staff": staff, "quote": quote, "csrf_token": request.state.staff_session.csrf_token,
                 "share_url": share_url},
    )
    return _private(response)


@router.get("/admin/quotations/{quote_id:uuid}/pdf")
def quotation_pdf_staff(
    quote_id: UUID,
    staff: Annotated[User, Depends(get_staff_page_user)],
    db: Session = Depends(get_db),
) -> Response:
    quote = _staff_quote_or_404(db, quote_id, staff)
    response = Response(render_quotation_pdf(quote), media_type="application/pdf")
    response.headers["Content-Disposition"] = f'attachment; filename="yatranest-quotation-{quote.id}.pdf"'
    return _private(response)


@router.get("/quotes/{quote_id:uuid}/{token}", response_class=HTMLResponse)
def quotation_shared_view(
    quote_id: UUID, token: str, request: Request, db: Session = Depends(get_db),
) -> HTMLResponse:
    quote = get_quotation_by_share_token(db, quote_id, token)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quotation link is invalid or expired.")
    response = templates.TemplateResponse(
        request=request, name="quotes/shared.html",
        context={"page_title": f"Travel quotation | YatraNest", "meta_description": "Private quotation details.",
                 "canonical_url": f"{str(request.base_url).rstrip('/')}/quotes/{quote.id}", "robots": "noindex, nofollow",
                 "quote": quote, "pdf_url": f"/quotes/{quote.id}/{token}/pdf"},
    )
    return _private(response)


@router.get("/quotes/{quote_id:uuid}/{token}/pdf")
def quotation_shared_pdf(quote_id: UUID, token: str, db: Session = Depends(get_db)) -> Response:
    quote = get_quotation_by_share_token(db, quote_id, token)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quotation link is invalid or expired.")
    response = Response(render_quotation_pdf(quote), media_type="application/pdf")
    response.headers["Content-Disposition"] = f'attachment; filename="yatranest-quotation-{quote.id}.pdf"'
    return _private(response)
