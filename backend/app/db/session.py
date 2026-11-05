from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# libpq waits forever by default when the DB host doesn't answer; fail fast instead.
# prepare_threshold=None disables psycopg3 client-side prepared statement cache, ensuring
# complete compatibility with Supabase Supavisor transaction pooler (port 6543).
engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 5, "prepare_threshold": None},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
