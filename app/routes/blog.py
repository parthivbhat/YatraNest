"""Admin/editor blog CMS and public server-rendered blog pages."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile

from app.database import get_db
from app.models import BlogPost, Package, User
from app.services.authorization import get_editor_page_user
from app.services.blog_cms import (
    MAX_COVER_BYTES,
    BlogValidationError,
    change_blog_publication,
    create_blog_post,
    get_blog_post_for_edit,
    make_slug,
    remove_new_cover,
    store_cover_image,
    update_blog_post,
)
from app.services.session_store import require_valid_csrf
from app.templating import templates


router = APIRouter(tags=["blog"])
PAGE_SIZE = 9


def _private(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _public_response(response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _form_context(request: Request, staff: User, db: Session, *, post: BlogPost | None,
                  values: dict[str, object], errors: list[str] | None = None) -> HTMLResponse:
    packages = list(db.scalars(
        select(Package).where(Package.status == "published").order_by(Package.title.asc())
    ))
    selected_ids = {str(package.id) for package in (post.related_packages if post else [])}
    response = templates.TemplateResponse(
        request=request,
        name="admin/blog_form.html",
        context={
            "page_title": "Blog post editor | YatraNest",
            "meta_description": "Create or edit a YatraNest travel article.",
            "staff": staff,
            "csrf_token": request.state.staff_session.csrf_token,
            "post": post,
            "values": values,
            "selected_package_ids": set(values.get("related_package_ids", selected_ids)),
            "packages": packages,
            "errors": errors or [],
        },
        status_code=422 if errors else 200,
    )
    return _private(response)


def _values_from_form(form) -> dict[str, object]:
    result = {}
    for name in ("title", "slug", "content", "category", "cover_image_alt", "seo_title", "seo_description"):
        value = form.get(name, "")
        result[name] = value if isinstance(value, str) else ""
    return result


def _package_ids_from_form(form) -> list[UUID]:
    ids: list[UUID] = []
    for value in form.getlist("related_package_ids"):
        try:
            ids.append(UUID(str(value)))
        except (ValueError, TypeError):
            raise BlogValidationError("One of the selected related packages is invalid.") from None
    return ids


async def _cover_from_form(form) -> str | None:
    upload = form.get("cover_image")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return None
    data = await upload.read(MAX_COVER_BYTES + 1)
    return store_cover_image(data, upload.content_type)


def _edit_values(post: BlogPost) -> dict[str, object]:
    return {name: getattr(post, name) or "" for name in (
        "title", "slug", "content", "category", "cover_image_alt", "seo_title", "seo_description"
    )}


@router.get("/admin/blog", response_class=HTMLResponse)
def blog_admin_list(
    request: Request,
    staff: Annotated[User, Depends(get_editor_page_user)],
    db: Session = Depends(get_db),
) -> HTMLResponse:
    posts = list(db.scalars(select(BlogPost).options(selectinload(BlogPost.author)).order_by(
        BlogPost.updated_at.desc(), BlogPost.id.desc()
    )))
    response = templates.TemplateResponse(
        request=request, name="admin/blog_list.html",
        context={"page_title": "Blog CMS | YatraNest", "meta_description": "Manage travel articles.",
                 "staff": staff, "posts": posts},
    )
    return _private(response)


@router.get("/admin/blog/new", response_class=HTMLResponse)
def blog_new(
    request: Request,
    staff: Annotated[User, Depends(get_editor_page_user)],
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _form_context(request, staff, db, post=None, values={
        "title": "", "slug": "", "content": "", "category": "", "cover_image_alt": "",
        "seo_title": "", "seo_description": "",
    })


async def _create_post(request: Request, staff: User, db: Session) -> Response:
    try:
        async with request.form(max_files=1, max_fields=30) as form:
            require_valid_csrf(request.state.staff_session, form.get("csrf_token"))
            values = _values_from_form(form)
            package_ids = _package_ids_from_form(form)
            values["related_package_ids"] = [str(item) for item in package_ids]
            saved_cover = await _cover_from_form(form)
        post = create_blog_post(db, actor=staff, fields=values, package_ids=package_ids,
                                cover_image_path=saved_cover)
        db.commit()
    except BlogValidationError as exc:
        db.rollback()
        if "saved_cover" in locals():
            remove_new_cover(saved_cover)
        return _form_context(request, staff, db, post=None, values=values if "values" in locals() else {}, errors=[str(exc)])
    except IntegrityError:
        db.rollback()
        if "saved_cover" in locals():
            remove_new_cover(saved_cover)
        return _form_context(request, staff, db, post=None, values=values if "values" in locals() else {},
                             errors=["The slug is already in use or the post could not be saved."])
    except Exception:
        db.rollback()
        if "saved_cover" in locals():
            remove_new_cover(saved_cover)
        raise
    return RedirectResponse(f"/admin/blog/{post.id}/preview", status_code=303)


@router.post("/admin/blog", response_class=HTMLResponse)
async def blog_create(
    request: Request,
    staff: Annotated[User, Depends(get_editor_page_user)],
    db: Session = Depends(get_db),
) -> Response:
    return await _create_post(request, staff, db)


@router.get("/admin/blog/{post_id:uuid}/edit", response_class=HTMLResponse)
def blog_edit(
    post_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_editor_page_user)],
    db: Session = Depends(get_db),
) -> HTMLResponse:
    post = get_blog_post_for_edit(db, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Blog post not found.")
    return _form_context(request, staff, db, post=post, values=_edit_values(post))


@router.post("/admin/blog/{post_id:uuid}/edit", response_class=HTMLResponse)
async def blog_update(
    post_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_editor_page_user)],
    db: Session = Depends(get_db),
) -> Response:
    saved_cover: str | None = None
    post = get_blog_post_for_edit(db, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Blog post not found.")
    try:
        async with request.form(max_files=1, max_fields=30) as form:
            require_valid_csrf(request.state.staff_session, form.get("csrf_token"))
            values = _values_from_form(form)
            package_ids = _package_ids_from_form(form)
            values["related_package_ids"] = [str(item) for item in package_ids]
            saved_cover = await _cover_from_form(form)
        post = update_blog_post(db, post_id=post_id, actor=staff, fields=values,
                                package_ids=package_ids, cover_image_path=saved_cover)
        db.commit()
    except BlogValidationError as exc:
        db.rollback()
        remove_new_cover(saved_cover)
        post = get_blog_post_for_edit(db, post_id)
        return _form_context(request, staff, db, post=post, values=values if "values" in locals() else _edit_values(post), errors=[str(exc)])
    except IntegrityError:
        db.rollback()
        remove_new_cover(saved_cover)
        post = get_blog_post_for_edit(db, post_id)
        return _form_context(request, staff, db, post=post, values=values if "values" in locals() else _edit_values(post),
                             errors=["The slug is already in use or the post could not be saved."])
    except Exception:
        db.rollback()
        remove_new_cover(saved_cover)
        raise
    return RedirectResponse(f"/admin/blog/{post.id}/preview", status_code=303)


@router.get("/admin/blog/{post_id:uuid}/preview", response_class=HTMLResponse)
def blog_preview(
    post_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_editor_page_user)],
    db: Session = Depends(get_db),
) -> HTMLResponse:
    post = get_blog_post_for_edit(db, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Blog post not found.")
    response = templates.TemplateResponse(
        request=request, name="blog/detail.html",
        context={"page_title": post.seo_title or post.title, "meta_description": post.seo_description or post.content[:160],
                 "canonical_url": f"{str(request.base_url).rstrip('/')}/blog/{post.slug}",
                 "robots": "noindex, nofollow", "post": post,
                 "related_packages": [p for p in post.related_packages if p.status == "published"],
                 "preview": True, "staff": staff, "csrf_token": request.state.staff_session.csrf_token},
    )
    return _private(response)


def _publication_action(post_id: UUID, request: Request, staff: User, db: Session, *, publish: bool) -> Response:
    from fastapi import HTTPException

    try:
        form = request.scope.get("_blog_action_form")
        require_valid_csrf(request.state.staff_session, form.get("csrf_token") if form else None)
        post = change_blog_publication(db, post_id=post_id, actor=staff, publish=publish)
        db.commit()
    except BlogValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 403, detail=str(exc)) from None
    return RedirectResponse(f"/admin/blog/{post.id}/preview" if publish else "/admin/blog", status_code=303)


@router.post("/admin/blog/{post_id:uuid}/publish")
async def blog_publish(
    post_id: UUID, request: Request,
    staff: Annotated[User, Depends(get_editor_page_user)], db: Session = Depends(get_db),
) -> Response:
    async with request.form(max_files=0, max_fields=2) as form:
        request.scope["_blog_action_form"] = form
        return _publication_action(post_id, request, staff, db, publish=True)


@router.post("/admin/blog/{post_id:uuid}/unpublish")
async def blog_unpublish(
    post_id: UUID, request: Request,
    staff: Annotated[User, Depends(get_editor_page_user)], db: Session = Depends(get_db),
) -> Response:
    async with request.form(max_files=0, max_fields=2) as form:
        request.scope["_blog_action_form"] = form
        return _publication_action(post_id, request, staff, db, publish=False)


@router.get("/blog", response_class=HTMLResponse)
def public_blog_list(
    request: Request,
    db: Session = Depends(get_db),
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    category: Annotated[str | None, Query(max_length=100)] = None,
) -> HTMLResponse:
    conditions = [BlogPost.status == "published"]
    if category:
        conditions.append(BlogPost.category == category)
    total = db.scalar(select(func.count(BlogPost.id)).where(*conditions)) or 0
    posts = list(db.scalars(
        select(BlogPost).where(*conditions).order_by(BlogPost.published_at.desc(), BlogPost.id.desc())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    ))
    categories = list(db.scalars(select(BlogPost.category).where(BlogPost.status == "published")
                                 .distinct().order_by(BlogPost.category)))
    response = templates.TemplateResponse(
        request=request, name="blog/index.html",
        context={"page_title": "Travel stories | YatraNest", "meta_description": "Travel ideas and practical guides from YatraNest.",
                 "canonical_url": f"{str(request.base_url).rstrip('/')}/blog", "robots": "index, follow",
                 "posts": posts, "categories": categories, "selected_category": category,
                 "page": page, "page_count": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)},
    )
    return _public_response(response)


@router.get("/blog/{slug}", response_class=HTMLResponse)
def public_blog_detail(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    post = db.scalar(select(BlogPost).where(BlogPost.slug == slug, BlogPost.status == "published")
                     .options(selectinload(BlogPost.related_packages)))
    if post is None:
        raise HTTPException(status_code=404, detail="Blog post not found.")
    related_packages = [package for package in post.related_packages if package.status == "published"]
    response = templates.TemplateResponse(
        request=request, name="blog/detail.html",
        context={"page_title": post.seo_title or post.title,
                 "meta_description": post.seo_description or post.content[:160],
                 "canonical_url": f"{str(request.base_url).rstrip('/')}/blog/{post.slug}",
                 "robots": "index, follow", "post": post, "related_packages": related_packages,
                 "preview": False, "staff": None},
    )
    return _public_response(response)
