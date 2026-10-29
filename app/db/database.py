"""Database engine, session management, and migration runner."""

from collections.abc import Generator
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _create_engine_from_url(url: str):
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    eng = create_engine(url, connect_args=connect_args, future=True)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return eng


engine = _create_engine_from_url(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_migrations(target_engine=None) -> None:
    """Apply SQL migrations and ensure all tables/indexes exist."""
    from app.db import models  # noqa: F401

    active_engine = target_engine or engine
    migration_file = Path(__file__).resolve().parents[2] / "migrations" / "001_initial_schema.sql"
    if migration_file.exists() and active_engine.dialect.name == "sqlite":
        sql_script = migration_file.read_text(encoding="utf-8")
        raw_conn = active_engine.raw_connection()
        try:
            raw_conn.executescript(sql_script)
            raw_conn.commit()
        finally:
            raw_conn.close()
    Base.metadata.create_all(bind=active_engine)
