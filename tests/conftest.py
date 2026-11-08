"""Require an isolated PostgreSQL database and provide rollback-only fixtures."""

import os
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


_test_database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
if not _test_database_url:
    raise pytest.UsageError(
        "TEST_DATABASE_URL is required to run tests. Set it to a dedicated PostgreSQL test database URL; "
        "tests will not use DATABASE_URL or the application's .env file."
    )

try:
    _parsed_test_url = make_url(_test_database_url)
except Exception:
    raise pytest.UsageError("TEST_DATABASE_URL is not a valid SQLAlchemy database URL.") from None

if _parsed_test_url.drivername != "postgresql+psycopg":
    raise pytest.UsageError("TEST_DATABASE_URL must point to PostgreSQL.")
if not _parsed_test_url.database:
    raise pytest.UsageError("TEST_DATABASE_URL must name a dedicated database.")
if _parsed_test_url.database.casefold() == "isocode_tours":
    raise pytest.UsageError(
        "TEST_DATABASE_URL points to the protected development database name. "
        "Use a separate PostgreSQL test database."
    )

# Prevent app.database from even constructing its normal engine from the
# development .env value during test imports. This assignment is confined to
# the pytest process and is sourced only from the required test variable.
os.environ["DATABASE_URL"] = _test_database_url

# The test engine is created directly from TEST_DATABASE_URL, regardless of
# whether application modules were imported by a pytest plugin beforehand.
import app.database as application_database  # noqa: E402

test_engine = create_engine(_test_database_url, pool_pre_ping=True)
application_database.engine.dispose()
application_database.engine = test_engine
if "app.main" in sys.modules:
    sys.modules["app.main"].engine = test_engine

TestSessionLocal = sessionmaker(
    bind=test_engine,
    class_=Session,
    autoflush=False,
    expire_on_commit=False,
)

# Existing tests import ``engine`` from app.database. Bind the session factory
# before test module collection so request sessions also use the isolated DB.
application_database.SessionLocal = TestSessionLocal


@pytest.fixture(scope="session", autouse=True)
def verify_test_database_connection():
    """Fail early if the dedicated PostgreSQL test database is unreachable."""
    try:
        with test_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        # Do not include the URL or driver exception, which can contain secrets.
        raise pytest.UsageError(
            "Could not connect to TEST_DATABASE_URL. Check that the dedicated PostgreSQL test database exists and is available."
        ) from None
    yield
    test_engine.dispose()


@pytest.fixture
def db_connection():
    """Keep all work on one connection inside a root transaction."""
    with test_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            if transaction.is_active:
                transaction.rollback()


@pytest.fixture
def db_session(db_connection):
    """Rollback test rows, including work committed through a savepoint."""
    session = Session(bind=db_connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def supplied_packages(db_session):
    """Create the two exact supplied package records within the test transaction."""
    from decimal import Decimal

    from app.models import Package, PackagePrice

    kerala = Package(
        package_code="PKG-KL-07",
        slug="munnar-thekkady-alleppey-houseboat",
        title="Munnar, Thekkady and Alleppey houseboat",
        duration_days=6,
        hotel_category="4-star",
        status="published",
        prices=[
            PackagePrice(
                season_name="Peak season",
                start_month=10,
                end_month=3,
                price_per_person=Decimal("32500.00"),
            ),
            PackagePrice(
                season_name="Monsoon season",
                start_month=6,
                end_month=9,
                price_per_person=Decimal("24900.00"),
            ),
        ],
    )
    south_india = Package(
        package_code="PKG-SI-12",
        slug="madurai-rameswaram-kanyakumari",
        title="Madurai, Rameswaram and Kanyakumari",
        duration_days=5,
        hotel_category="3-star",
        status="draft",
        prices=[
            PackagePrice(
                season_name="All year",
                start_month=1,
                end_month=12,
                price_per_person=Decimal("18750.00"),
            ),
        ],
    )
    db_session.add_all([kerala, south_india])
    db_session.flush()
    return {"kerala": kerala, "south_india": south_india}
