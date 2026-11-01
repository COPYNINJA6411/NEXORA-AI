"""Database URL handling and SQLAlchemy engine/session helpers."""

from __future__ import annotations

import os
import json
import re
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from dotenv import load_dotenv

load_dotenv()

DEFAULT_DATABASE_URL = "sqlite:///./data/nexora_demo.sqlite3"
DATASET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def configured_datasets() -> dict[str, str]:
    """Return configured dataset URLs. Never include these URLs in API responses or logs."""
    raw = os.getenv("NEXORA_DATASETS_JSON", "").strip()
    if raw:
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("NEXORA_DATASETS_JSON must be a JSON object of dataset IDs to SQLAlchemy URLs") from exc
        if not isinstance(values, dict) or not values:
            raise ValueError("NEXORA_DATASETS_JSON must contain at least one dataset")
        for dataset_id, url in values.items():
            if not isinstance(dataset_id, str) or not DATASET_ID_PATTERN.fullmatch(dataset_id):
                raise ValueError("Dataset IDs may contain only letters, numbers, underscores, and hyphens")
            if not isinstance(url, str) or not url:
                raise ValueError(f"Dataset '{dataset_id}' has an invalid database URL")
        return values
    return {"demo": os.getenv("NEXORA_DATABASE_URL", DEFAULT_DATABASE_URL)}


def database_url(dataset_id: str | None = None) -> str:
    """Resolve a configured dataset URL without logging or exposing credentials."""
    datasets = configured_datasets()
    selected = dataset_id or os.getenv("NEXORA_DATASET", "demo")
    try:
        return datasets[selected]
    except KeyError as exc:
        raise ValueError(f"Dataset '{selected}' is not configured") from exc


def make_database_engine(
    url: str | None = None,
    *,
    dataset_id: str | None = None,
    read_only: bool = False,
) -> Engine:
    selected_url = url or database_url(dataset_id)
    parsed = make_url(selected_url)
    connect_args: dict[str, object] = {}
    if parsed.get_backend_name() == "sqlite":
        if read_only:
            if not parsed.database or parsed.database == ":memory:":
                raise ValueError("SQLite read-only connections require a file-backed database.")
            uri_path = parsed.database.replace("\\", "/")
            if not uri_path.startswith("file:"):
                uri_path = f"file:{uri_path}"
            parsed = parsed.set(database=uri_path, query={**parsed.query, "mode": "ro", "uri": "true"})
            connect_args["uri"] = True
        elif parsed.database and parsed.database != ":memory:":
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 30
    engine = create_engine(parsed, echo=False, pool_pre_ping=True, connect_args=connect_args)

    if parsed.get_backend_name() == "sqlite" and not read_only:
        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
