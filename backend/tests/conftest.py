import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from app.core.config import Settings


def _test_database_url() -> str:
    """TEST_DATABASE_URL if set, else DATABASE_URL with '_test' appended to the database name."""
    dev_url = make_url(Settings().database_url)
    explicit = os.environ.get("TEST_DATABASE_URL")
    url = make_url(explicit) if explicit else dev_url.set(database=f"{dev_url.database}_test")
    if url == dev_url:
        raise RuntimeError("Refusing to run tests against the development database")
    return url.render_as_string(hide_password=False)


# Must happen before any app module builds the engine or caches settings.
os.environ["DATABASE_URL"] = _test_database_url()
os.environ["ENVIRONMENT"] = "test"
# Tests never reach a real AI provider, whatever backend/.env says. Live tests opt in separately.
os.environ["AI_PROVIDERS"] = ""

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db.session import engine, get_db  # noqa: E402
from app.main import app  # noqa: E402
from tests.auth_helpers import api_client  # noqa: E402
from app.api.routes.auth import get_supabase_service  # noqa: E402
from tests.fake_supabase import FakeSupabaseAuthService  # noqa: E402

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    command.upgrade(Config(str(ALEMBIC_INI)), "head")


@pytest.fixture(autouse=True)
def fake_supabase() -> Iterator[FakeSupabaseAuthService]:
    fake = FakeSupabaseAuthService()
    app.dependency_overrides[get_supabase_service] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_supabase_service, None)


@pytest.fixture(autouse=True)
def fresh_usage_limits() -> Iterator[None]:
    """Usage counters written on committed connections (race tests) outlive their test, and every
    TestClient shares one network key: start each test with no usage counted."""
    yield
    with engine.begin() as conn:
        if conn.scalar(text("SELECT to_regclass('usage_counters')")) is not None:
            conn.execute(text("DELETE FROM usage_counters"))
        if conn.scalar(text("SELECT to_regclass('provider_calls')")) is not None:
            conn.execute(text("DELETE FROM provider_calls"))


@pytest.fixture
def db_session() -> Iterator[Session]:
    """Session inside an outer transaction that is always rolled back.

    App-level commits/rollbacks become SAVEPOINT operations, so nothing a test writes persists.
    """
    with engine.connect() as conn:
        outer = conn.begin()
        session = Session(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            session.close()
            outer.rollback()


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    def _get_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    try:
        yield api_client()
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def dead_database_client() -> Iterator[TestClient]:
    dead = create_engine(
        "postgresql+psycopg://nobody:secretpw@127.0.0.1:1/nowhere",
        connect_args={"connect_timeout": 2},
    )

    def _get_db() -> Iterator[Session]:
        with Session(dead) as s:
            yield s

    app.dependency_overrides[get_db] = _get_db
    try:
        # Any session cookie: authentication itself needs the database, so it must fail as 503.
        yield api_client("any-token")
    finally:
        app.dependency_overrides.pop(get_db, None)
        dead.dispose()
