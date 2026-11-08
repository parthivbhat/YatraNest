"""Server-rendered public pages and SEO endpoints."""

from calendar import month_name
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AISuggestion, Package
from app.services.ai_extraction_lifecycle import ensure_initial_suggestion, process_extraction
from app.services.public_enquiries import (
    MAX_FORM_BYTES,
    consume_form_nonce,
    enquiry_from_form,
    issue_form_nonce,
    parse_public_enquiry_form,
    valid_form_nonce,
)
from app.services.public_packages import (
    get_published_package,
    published_packages_statement,
    search_published_packages,
)
from app.services.destination_media import destination_cards_for_packages
from app.services.blog_cms import latest_published_blog_posts
from app.templating import templates


router = APIRouter()
SITEMAP_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"


def _run_extraction_background(suggestion_id: UUID) -> None:
    """Contain unexpected worker errors after the public response is sent."""
    try:
        process_extraction(suggestion_id)
    except Exception:
        # Background failures must not propagate through the response lifecycle.
        # The worker itself stores only approved safe failure codes when possible.
        return


def _mark_unscheduled_extraction_failed(db: Session, suggestion_id: UUID) -> None:
    """Best-effort, conditional retryable state for work the app could not enqueue."""
    try:
        db.execute(
            update(AISuggestion)
            .where(
                AISuggestion.id == suggestion_id,
                AISuggestion.generation_status == "pending",
                AISuggestion.generation_attempt_id.is_(None),
            )
            .values(
                generation_status="failed",
                failure_code="generation_failed",
                updated_at=func.now(),
            )
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            # This is best effort after the enquiry has already committed.
            pass


def _canonical_url(request: Request, path: str) -> str:
    """Build a canonical URL from the current request origin and known route path."""
    return f"{str(request.base_url).rstrip('/')}{path}"


def _same_origin_form_request(request: Request) -> bool:
    """Reject cross-site browser posts using origin and fetch metadata checks."""
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return False
    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer")
        if not referer:
            return False
        origin = referer
    received = urlsplit(origin)
    expected = urlsplit(str(request.base_url))
    return bool(
        received.scheme
        and received.netloc
        and received.scheme.lower() == expected.scheme.lower()
        and received.netloc.lower() == expected.netloc.lower()
    )


def _render_package_detail(
    request: Request,
    package: Package,
    *,
    form_nonce: str,
    form_values: dict[str, object] | None = None,
    form_errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request,
        name="packages/detail.html",
        context={
            "page_title": f"{package.title} | YatraNest",
            "meta_description": package.summary or f"Explore {package.title} with YatraNest.",
            "canonical_url": _canonical_url(request, f"/packages/{package.slug}"),
            "package": package,
            "form_nonce": form_nonce,
            "form_values": form_values or {},
            "form_errors": form_errors or {},
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _optional_integer(
    value: str | None,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int | None:
    """Treat blank optional query values as absent and validate nonblank values."""
    if value is None or not value.strip():
        return None

    try:
        parsed_value = int(value.strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} must be an integer.") from exc

    if not minimum <= parsed_value <= maximum:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be between {minimum} and {maximum}.",
        )
    return parsed_value


@router.get("/", response_class=HTMLResponse, name="home")
def home(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Render the public homepage and a featured published package, if available."""
    published = list(
        db.scalars(
        published_packages_statement()
        .order_by(Package.published_at.desc().nullslast(), Package.title.asc())
        )
    )
    featured = published[0] if published else None
    recent_blog_posts = latest_published_blog_posts(db, limit=3)
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "page_title": "Thoughtful journeys through southern India | YatraNest",
            "meta_description": "Discover southern India with YatraNest, a travel platform based in Kochi, Kerala.",
            "canonical_url": _canonical_url(request, "/"),
            "featured_package": featured,
            "destination_cards": destination_cards_for_packages(published),
            "recent_blog_posts": recent_blog_posts,
        },
    )


@router.get("/packages", response_class=HTMLResponse, name="package_list")
def package_list(
    request: Request,
    db: Session = Depends(get_db),
    q: Annotated[str | None, Query(max_length=100)] = None,
    month: Annotated[str | None, Query(max_length=4)] = None,
    year: Annotated[str | None, Query(max_length=4)] = None,
) -> HTMLResponse:
    """List only published packages, with optional text and seasonal filters."""
    month = _optional_integer(month, field_name="month", minimum=1, maximum=12)
    year = _optional_integer(year, field_name="year", minimum=2000, maximum=2100)
    if year is not None and month is None:
        raise HTTPException(status_code=422, detail="Choose a travel month with the year.")

    packages = search_published_packages(db, query=q, travel_month=month, travel_year=year)
    return templates.TemplateResponse(
        request=request,
        name="packages/list.html",
        context={
            "page_title": "Travel packages | YatraNest",
            "meta_description": "Browse published journeys from YatraNest, based in Kochi, Kerala.",
            "canonical_url": _canonical_url(request, "/packages"),
            "packages": packages,
            "query": (q or "").strip(),
            "selected_month": month,
            "selected_year": year,
            "months": [(number, month_name[number]) for number in range(1, 13)],
        },
    )


@router.get("/packages/{slug}", response_class=HTMLResponse, name="package_detail")
def package_detail(request: Request, slug: str, db: Session = Depends(get_db)) -> HTMLResponse:
    """Render a published package page; unpublished slugs are intentionally hidden."""
    package = get_published_package(db, slug)
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found")

    form_nonce = issue_form_nonce(db, package)
    db.commit()
    return _render_package_detail(request, package, form_nonce=form_nonce)


@router.post("/packages/{slug}/enquiries", response_class=HTMLResponse, name="submit_package_enquiry")
async def submit_package_enquiry(
    request: Request,
    slug: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Persist a traveller enquiry for a currently published package only."""
    package = get_published_package(db, slug)
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found")

    if not _same_origin_form_request(request):
        raise HTTPException(status_code=403, detail="Form origin could not be verified.")

    try:
        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
            raise HTTPException(status_code=413, detail="The submitted form is too large.")
        form = await request.form(max_files=0, max_fields=20, max_part_size=MAX_FORM_BYTES)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="The enquiry form could not be read.") from exc

    # Silently reject automated honeypot submissions without storing user data.
    if str(form.get("website", "")).strip():
        raise HTTPException(status_code=400, detail="The enquiry form could not be accepted.")

    raw_nonce = form.get("form_nonce")
    form_nonce = raw_nonce if isinstance(raw_nonce, str) else ""
    if not valid_form_nonce(db, package, form_nonce):
        db.rollback()
        raise HTTPException(status_code=403, detail="This form has expired or was already submitted. Reload the package page.")

    values, errors = parse_public_enquiry_form(form)
    if not errors:
        if not consume_form_nonce(db, package, form_nonce):
            db.rollback()
            raise HTTPException(status_code=403, detail="This form has expired or was already submitted. Reload the package page.")

        try:
            enquiry = enquiry_from_form(package, values)
            db.add(enquiry)
            suggestion = ensure_initial_suggestion(db, enquiry)
            suggestion_id = suggestion.id
            should_schedule_extraction = suggestion.generation_status == "pending"
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise HTTPException(status_code=503, detail="The enquiry could not be saved. Please try again.") from None
        except (RuntimeError, ValueError):
            db.rollback()
            raise HTTPException(status_code=503, detail="The enquiry could not be saved. Please try again.") from None

        if should_schedule_extraction:
            try:
                background_tasks.add_task(_run_extraction_background, suggestion_id)
            except Exception:
                # The enquiry, nonce and lifecycle row are already committed.
                # Keep the browser acknowledgement successful and make the row
                # eligible for the later authorized staff retry workflow.
                try:
                    _mark_unscheduled_extraction_failed(db, suggestion_id)
                except Exception:
                    # Failure to persist the operational marker cannot undo or
                    # suppress the already-committed customer acknowledgement.
                    pass
        response = templates.TemplateResponse(
            request=request,
            name="packages/enquiry_confirmation.html",
            context={
                "page_title": "Enquiry received | YatraNest",
                "meta_description": "Your enquiry has been received.",
                "canonical_url": _canonical_url(request, f"/packages/{package.slug}"),
                "robots": "noindex, nofollow",
            },
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    response = _render_package_detail(
        request,
        package,
        form_nonce=form_nonce,
        form_values={key: value for key, value in form.multi_items() if key != "form_nonce"},
        form_errors=errors,
        status_code=422,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/sitemap.xml", name="sitemap")
def sitemap(request: Request, db: Session = Depends(get_db)) -> Response:
    """Generate an XML sitemap from published package slugs only."""
    packages = list(
        db.scalars(
            select(Package)
            .where(Package.status == "published")
            .where(Package.slug.is_not(None))
            .order_by(Package.slug.asc())
        )
    )
    urlset = Element("urlset", xmlns=SITEMAP_NAMESPACE)
    locations = [_canonical_url(request, "/"), _canonical_url(request, "/packages")]
    locations.extend(
        _canonical_url(request, f"/packages/{package.slug}")
        for package in packages
        if package.slug
    )
    for location in locations:
        item = SubElement(urlset, f"{{{SITEMAP_NAMESPACE}}}url")
        SubElement(item, f"{{{SITEMAP_NAMESPACE}}}loc").text = location

    xml_body = tostring(urlset, encoding="utf-8", xml_declaration=True)
    return Response(content=xml_body, media_type="application/xml")
