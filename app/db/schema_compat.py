from __future__ import annotations

from collections.abc import Iterable

from flask import current_app
from sqlalchemy import inspect, text

from app.extensions import db


PROCESSING_JOB_COLUMNS: dict[str, str] = {
    "job_kind": "VARCHAR(32) NOT NULL DEFAULT 'conversion'",
    "selected_sheet": "VARCHAR(255)",
    "execution_mode": "VARCHAR(32)",
    "resume_step": "INTEGER NOT NULL DEFAULT 1",
    "assistant_state": "JSON NOT NULL DEFAULT '{}'",
}

AI_SETTINGS_COLUMNS: dict[str, str] = {
    "memory_mode": "VARCHAR(32) NOT NULL DEFAULT 'economy'",
    "idle_unload_seconds": "INTEGER NOT NULL DEFAULT 300",
    "selection_mode": "VARCHAR(32) NOT NULL DEFAULT 'auto'",
    "auto_activate": "BOOLEAN NOT NULL DEFAULT 1",
    "auto_test": "BOOLEAN NOT NULL DEFAULT 1",
    "reselect_if_unavailable": "BOOLEAN NOT NULL DEFAULT 1",
    "min_free_ram_gb": "FLOAT NOT NULL DEFAULT 2",
    "max_ram_usage_ratio": "FLOAT NOT NULL DEFAULT 0.72",
    "auto_selection_reason": "TEXT NOT NULL DEFAULT ''",
    "hardware_fingerprint": "VARCHAR(128) NOT NULL DEFAULT ''",
    "last_test_model_signature": "VARCHAR(256) NOT NULL DEFAULT ''",
}

REQUIRED_AI_SETTINGS_COLUMNS = frozenset(AI_SETTINGS_COLUMNS)
REQUIRED_PROCESSING_JOB_COLUMNS = frozenset(PROCESSING_JOB_COLUMNS)


def table_columns(table_name: str) -> set[str]:
    inspector = inspect(db.engine)
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def schema_has_columns(table_name: str, required: Iterable[str]) -> bool:
    return set(required).issubset(table_columns(table_name))


def ensure_local_sqlite_schema_compatibility() -> list[str]:
    """Add known additive columns to an older local SQLite database.

    This is a local-development compatibility bridge, not a replacement for
    Alembic migrations. It is intentionally limited to additive columns from
    revisions 0003 and 0004 and only runs for SQLite when explicitly enabled.
    """

    if not current_app.config.get("AUTO_REPAIR_SQLITE_SCHEMA", True):
        return []

    database_uri = str(current_app.config.get("SQLALCHEMY_DATABASE_URI", ""))
    if not database_uri.startswith("sqlite:"):
        return []

    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())
    repaired: list[str] = []

    plans = (
        ("processing_jobs", PROCESSING_JOB_COLUMNS),
        ("ai_settings", AI_SETTINGS_COLUMNS),
    )
    with db.engine.begin() as connection:
        for table_name, definitions in plans:
            if table_name not in table_names:
                continue
            existing = {
                column["name"]
                for column in inspect(connection).get_columns(table_name)
            }
            for column_name, ddl in definitions.items():
                if column_name in existing:
                    continue
                connection.execute(
                    text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {ddl}')
                )
                repaired.append(f"{table_name}.{column_name}")
                existing.add(column_name)

    return repaired
