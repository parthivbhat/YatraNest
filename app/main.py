"""FastAPI application entry point."""

from pathlib import Path

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.database import engine
from app.middleware.public_form_limit import PublicEnquiryBodyLimitMiddleware
from app.middleware.blog_upload_limit import BlogUploadBodyLimitMiddleware
from app.routes.admin import router as admin_router
from app.routes.auth import router as auth_router
from app.routes.blog import router as blog_router
from app.routes.public import router as public_router
from app.routes.quotations import router as quotations_router
from app.routes.staff_management import router as staff_management_router


BASE_DIR = Path(__file__).resolve().parent.parent
app = FastAPI(title="YatraNest", version="0.1.0")
app.add_middleware(PublicEnquiryBodyLimitMiddleware)
app.add_middleware(BlogUploadBodyLimitMiddleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.include_router(public_router)
app.include_router(blog_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(quotations_router)
app.include_router(staff_management_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the web application process is responding."""
    return {"status": "ok"}


@app.get("/health/db", response_model=None)
def database_health() -> dict[str, str] | JSONResponse:
    """Run a lightweight query so this check verifies real database connectivity."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "database": "unavailable"},
        )
    return {"status": "ok", "database": "connected"}
