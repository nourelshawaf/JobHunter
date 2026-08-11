"""Alembic migration environment — wired to jobhunter models and settings."""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# ── Bootstrap import path ─────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobhunter.config import get_settings
from jobhunter.database import Base

# Ensure data/ directory exists before SQLite tries to open a file in it.
# Without this, Alembic fails on first run if data/ doesn't exist yet.
_settings_early = get_settings()
if _settings_early.is_sqlite:
    _settings_early.data_dir.mkdir(parents=True, exist_ok=True)

# Import every model so autogenerate sees all tables
from jobhunter.models import application, job, notification, profile  # noqa: F401

alembic_cfg = context.config
settings = get_settings()

# Override the URL from our settings (reads DATABASE_URL from .env)
alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)

if alembic_cfg.config_file_name is not None:
    fileConfig(alembic_cfg.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL without a live connection (useful for review / CI)."""
    url = alembic_cfg.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    # Re-read URL to pick up any env changes at runtime
    cfg_section = alembic_cfg.get_section(alembic_cfg.config_ini_section, {})
    cfg_section["sqlalchemy.url"] = settings.database_url

    connectable = engine_from_config(
        cfg_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # migrations never need pooling
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
