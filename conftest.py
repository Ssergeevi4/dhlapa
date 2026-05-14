import asyncio
import sys

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from src.main import app
from config.settings import settings
from db.models.base import BaseModel
from db.models.subscription import PromoCodeModel
from db.models.admin_user import AdminUserModel
import db.models.user  # noqa: F401
import db.models.user_device  # noqa: F401
import db.models.notification_outbox  # noqa: F401
import db.models.organization  # noqa: F401
import db.models.subscription  # noqa: F401
import db.models.admin_audit_log  # noqa: F401
import db.models.client_owner_history  # noqa: F401
import db.models.article  # noqa: F401
import db.models.telemetry_event  # noqa: F401

# Use localhost for local test runs (when not in Docker), use 'db' when in Docker container
_db_host = settings.DB_HOST
if settings.DB_HOST == "db" and settings.ENVIRONMENT != "prod":
    # Running tests locally outside Docker - use localhost instead
    try:
        import socket
        socket.gethostbyname("db")
    except (socket.gaierror, OSError):
        _db_host = "localhost"

TEST_DB_URL = (
    f"postgresql+asyncpg://{settings.DB_USER}:{settings.DB_PASS}@"
    f"{_db_host}:{settings.DB_PORT}/{settings.DB_NAME}"
)


def _is_no_db_item(item) -> bool:
    return item.get_closest_marker("no_db") is not None


def pytest_collection_modifyitems(config, items):
    config._skip_db_setup = bool(items) and all(_is_no_db_item(item) for item in items)


@pytest.fixture(scope="session", autouse=True)
def setup_db(request):
    """Recreate public schema for tests to avoid FK dependency drop issues."""
    if getattr(request.config, "_skip_db_setup", False):
        yield
        return

    async def _create():
        engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
            await conn.run_sync(BaseModel.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            # Promo code
            session.add(PromoCodeModel(code="TESTPROMO", days=30, is_active=True))
            # Test admin users
            import bcrypt
            password_hash = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
            session.add(AdminUserModel(
                email="superadmin@test.com",
                password_hash=password_hash,
                role="SuperAdmin",
                status="active",
            ))
            session.add(AdminUserModel(
                email="editor@test.com",
                password_hash=password_hash,
                role="ContentEditor",
                status="active",
            ))
            session.add(AdminUserModel(
                email="support@test.com",
                password_hash=password_hash,
                role="TechSupport",
                status="active",
            ))
            await session.commit()

        await engine.dispose()

    async def _drop():
        engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
        await engine.dispose()

    asyncio.run(_create())
    yield
    asyncio.run(_drop())


pytest_plugins = [
    "tests.fixtures.usecase.booking_request",
    "tests.fixtures.infrastructure",
]

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

@pytest_asyncio.fixture
async def async_client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


