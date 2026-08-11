"""
Database setup.

Supports SQLite (development/testing) and PostgreSQL (production)
via a single DATABASE_URL environment variable.

PostgreSQL features enabled when DATABASE_URL starts with postgresql:
- Connection pooling (QueuePool) with configurable size
- pool_pre_ping: stale connections are discarded before use
- Connection recycling every 30 minutes
- Automatic retry on connection loss

SQLite features:
- WAL journal mode (better read concurrency)
- Foreign key enforcement
- check_same_thread=False (required for FastAPI/Streamlit threads)

Engine is created lazily (on first import) and is a module-level singleton.
Use get_engine() to access it — do not import `engine` directly in tests
because it freezes the URL at import time.
"""
from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache
from typing import Any

import structlog
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from jobhunter.config import get_settings

logger = structlog.get_logger(__name__)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


# ── Engine factory ────────────────────────────────────────────────────────

def _build_engine(url: str, is_sqlite: bool, is_test: bool = False) -> Any:
    """
    Build a SQLAlchemy engine with appropriate pool settings.

    Args:
        url:       Database URL string.
        is_sqlite: True when the URL is a SQLite connection.
        is_test:   True for in-memory/testing databases (uses NullPool).
    """
    if is_sqlite:
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            echo=False,
            poolclass=NullPool if "memory" in url else None,  # type: ignore[arg-type]
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _: Any) -> None:
            """Enable WAL mode and foreign keys for every SQLite connection."""
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
            dbapi_conn.execute("PRAGMA synchronous=NORMAL")

        logger.info("database.engine_created", dialect="sqlite", url=url[:60])
        return engine

    # PostgreSQL — full connection pooling
    pool_kwargs: dict[str, Any] = {
        "poolclass": QueuePool,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout,
        "pool_recycle": settings.db_pool_recycle,
        "pool_pre_ping": True,
        "echo": False,
    }

    engine = create_engine(url, **pool_kwargs)
    logger.info(
        "database.engine_created",
        dialect="postgresql",
        pool_size=pool_kwargs["pool_size"],
        max_overflow=pool_kwargs["max_overflow"],
    )
    return engine


@lru_cache(maxsize=1)
def get_engine() -> Any:
    """Return the cached SQLAlchemy engine (singleton per process)."""
    settings = get_settings()
    return _build_engine(
        url=settings.database_url,
        is_sqlite=settings.is_sqlite,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker:  # type: ignore[type-arg]
    """Return the cached SessionLocal factory."""
    return sessionmaker(
        bind=get_engine(),
        autocommit=False,
        autoflush=False,
    )


def SessionLocal() -> Session:  # noqa: N802  (kept for API compatibility)
    """Create a new database session. Caller is responsible for closing."""
    return get_session_factory()()


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI / dependency-injection context manager that yields a Session.

    Usage::

        @app.get("/jobs")
        def list_jobs(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """
    Create all tables that don't yet exist.

    Safe to call on every startup — only creates missing tables,
    never drops or alters existing ones.
    Alembic handles schema migrations; this handles bare bootstrapping.
    """
    # Import every model so SQLAlchemy registers them with Base.metadata
    from jobhunter.models import application, job, notification, profile  # noqa: F401

    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    logger.info("database.init_db.done", dialect="sqlite" if settings.is_sqlite else "postgresql")


def check_connection() -> bool:
    """
    Verify the database is reachable.

    Returns True on success, False on any error.
    Used by the health-check endpoint and the scheduler startup.
    """
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("database.connection_failed", error=str(exc))
        return False
