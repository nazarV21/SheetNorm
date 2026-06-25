from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import inspect

from app import create_app
from app.db.schema_compat import (
    REQUIRED_AI_SETTINGS_COLUMNS,
    REQUIRED_PROCESSING_JOB_COLUMNS,
)
from app.extensions import db
from config import Config


def _create_legacy_schema(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE processing_jobs (
                id VARCHAR(36) PRIMARY KEY,
                status VARCHAR(32) NOT NULL DEFAULT 'created',
                progress INTEGER NOT NULL DEFAULT 0,
                stage VARCHAR(120) NOT NULL DEFAULT 'created',
                error_details JSON NOT NULL DEFAULT '{}',
                created_at DATETIME NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                input_filename VARCHAR(512),
                input_path TEXT,
                output_filename VARCHAR(512),
                output_path TEXT,
                rule_id VARCHAR(36),
                rule_name VARCHAR(255),
                original_instruction TEXT,
                improved_instruction TEXT
            );
            CREATE TABLE ai_settings (
                id VARCHAR(36) PRIMARY KEY,
                backend VARCHAR(32) NOT NULL DEFAULT 'fallback',
                selected_model_relative_path VARCHAR(1024),
                active_model_relative_path VARCHAR(1024),
                performance_profile VARCHAR(32) NOT NULL DEFAULT 'balanced',
                context_tokens INTEGER NOT NULL DEFAULT 4096,
                max_completion_tokens INTEGER NOT NULL DEFAULT 1000,
                n_threads INTEGER NOT NULL DEFAULT 4,
                n_batch INTEGER NOT NULL DEFAULT 128,
                n_gpu_layers INTEGER NOT NULL DEFAULT 0,
                temperature FLOAT NOT NULL DEFAULT 0.15,
                last_test_status VARCHAR(32) NOT NULL DEFAULT 'not_tested',
                last_test_message TEXT NOT NULL DEFAULT '',
                last_tested_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_old_local_sqlite_schema_is_repaired_before_runtime_queries(tmp_path: Path):
    database_path = tmp_path / "legacy.db"
    _create_legacy_schema(database_path)

    class LegacyConfig(Config):
        TESTING = True
        DATA_STORE_BACKEND = "database"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path.as_posix()}"
        AUTO_CREATE_SQLITE_DB = True
        AUTO_REPAIR_SQLITE_SCHEMA = True
        AI_AUTO_SELECT_ON_STARTUP = False
        WTF_CSRF_ENABLED = False

    app = create_app(LegacyConfig)
    with app.app_context():
        inspector = inspect(db.engine)
        job_columns = {column["name"] for column in inspector.get_columns("processing_jobs")}
        ai_columns = {column["name"] for column in inspector.get_columns("ai_settings")}

    assert REQUIRED_PROCESSING_JOB_COLUMNS <= job_columns
    assert REQUIRED_AI_SETTINGS_COLUMNS <= ai_columns


def test_schema_status_command_reports_compatible_database(tmp_path: Path):
    database_path = tmp_path / "status.db"

    class StatusConfig(Config):
        TESTING = True
        DATA_STORE_BACKEND = "database"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path.as_posix()}"
        AUTO_CREATE_SQLITE_DB = True
        AUTO_REPAIR_SQLITE_SCHEMA = True
        AI_AUTO_SELECT_ON_STARTUP = False
        WTF_CSRF_ENABLED = False

    app = create_app(StatusConfig)
    result = app.test_cli_runner().invoke(args=["db-schema-status"])

    assert result.exit_code == 0
    assert "Schema is compatible." in result.output
    assert "Missing processing_jobs columns: none" in result.output
    assert "Missing ai_settings columns: none" in result.output


def test_ai_settings_falls_back_without_querying_missing_columns(tmp_path: Path, caplog):
    from app.services.ai.settings_service import AISettingsService

    database_path = tmp_path / "legacy_settings.db"
    _create_legacy_schema(database_path)

    class LegacySettingsConfig(Config):
        TESTING = True
        DATA_STORE_BACKEND = "database"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path.as_posix()}"
        AUTO_CREATE_SQLITE_DB = False
        AUTO_REPAIR_SQLITE_SCHEMA = False
        AI_AUTO_SELECT_ON_STARTUP = False
        AI_SETTINGS_FILE = tmp_path / "ai_settings.json"
        WTF_CSRF_ENABLED = False

    app = create_app(LegacySettingsConfig)
    with app.app_context():
        settings = AISettingsService().get()

    assert settings.selection_mode == "auto"
    assert "no such column" not in caplog.text.lower()


def test_schema_repair_migration_upgrades_partially_stamped_database(tmp_path: Path):
    database_path = tmp_path / "partially_upgraded.db"
    _create_legacy_schema(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version(version_num) VALUES ('0004_auto_model_selection')")
        connection.commit()
    finally:
        connection.close()

    class MigrationConfig(Config):
        TESTING = True
        DATA_STORE_BACKEND = "database"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path.as_posix()}"
        AUTO_CREATE_SQLITE_DB = False
        AUTO_REPAIR_SQLITE_SCHEMA = False
        AI_AUTO_SELECT_ON_STARTUP = True
        AI_SETTINGS_FILE = tmp_path / "ai_settings.json"
        WTF_CSRF_ENABLED = False

    app = create_app(MigrationConfig)
    result = app.test_cli_runner().invoke(args=["db", "upgrade"])

    assert result.exit_code == 0, result.output
    with app.app_context():
        inspector = inspect(db.engine)
        job_columns = {column["name"] for column in inspector.get_columns("processing_jobs")}
        ai_columns = {column["name"] for column in inspector.get_columns("ai_settings")}

    assert REQUIRED_PROCESSING_JOB_COLUMNS <= job_columns
    assert REQUIRED_AI_SETTINGS_COLUMNS <= ai_columns
