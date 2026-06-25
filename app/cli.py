from __future__ import annotations

import click
from flask import Flask, current_app

from app.db.legacy_import import import_legacy_json
from app.db.models import User, Workspace, WorkspaceMember
from app.extensions import db


def register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db() -> None:
        db.create_all()
        click.echo("Database tables created.")

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.option("--name", default="Administrator", show_default=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    @click.option("--workspace", default="default", show_default=True)
    def create_admin(email: str, name: str, password: str, workspace: str) -> None:
        user = User.query.filter_by(email=email.lower()).one_or_none()
        if user is None:
            user = User(email=email.lower(), name=name, is_superuser=True)
            db.session.add(user)
        user.name = name
        user.is_active = True
        user.is_superuser = True
        user.set_password(password)

        workspace_row = Workspace.query.filter_by(slug=workspace).one_or_none()
        if workspace_row is None:
            workspace_row = Workspace(name=workspace.title(), slug=workspace, settings_json={})
            db.session.add(workspace_row)
            db.session.flush()

        db.session.flush()
        membership = WorkspaceMember.query.filter_by(workspace_id=workspace_row.id, user_id=user.id).one_or_none()
        if membership is None:
            db.session.add(WorkspaceMember(workspace_id=workspace_row.id, user_id=user.id, role="admin"))
        else:
            membership.role = "admin"
        db.session.commit()
        click.echo(f"Admin user ready: {email.lower()}")

    @app.cli.command("import-json")
    @click.option("--dry-run", is_flag=True, help="Validate import without committing.")
    @click.option("--workspace", default="default", show_default=True)
    def import_json(dry_run: bool, workspace: str) -> None:
        summary = import_legacy_json(
            rules_file=current_app.config["RULES_FILE"],
            jobs_file=current_app.config["JOBS_FILE"],
            training_examples_file=current_app.config["TRAINING_EXAMPLES_FILE"],
            workspace_slug=workspace,
            dry_run=dry_run,
        )
        click.echo(summary)


    @app.cli.command("db-schema-status")
    def db_schema_status() -> None:
        """Show the actual database revision and required compatibility columns."""
        from sqlalchemy import inspect, text

        from app.db.schema_compat import (
            REQUIRED_AI_SETTINGS_COLUMNS,
            REQUIRED_PROCESSING_JOB_COLUMNS,
            table_columns,
        )

        inspector = inspect(db.engine)
        tables = set(inspector.get_table_names())
        revision = "not stamped"
        if "alembic_version" in tables:
            row = db.session.execute(text("select version_num from alembic_version")).first()
            revision = row[0] if row else "empty"

        missing_jobs = sorted(REQUIRED_PROCESSING_JOB_COLUMNS - table_columns("processing_jobs"))
        missing_ai = sorted(REQUIRED_AI_SETTINGS_COLUMNS - table_columns("ai_settings"))
        click.echo(f"Database: {db.engine.url.render_as_string(hide_password=True)}")
        click.echo(f"Alembic revision: {revision}")
        click.echo(f"Missing processing_jobs columns: {', '.join(missing_jobs) or 'none'}")
        click.echo(f"Missing ai_settings columns: {', '.join(missing_ai) or 'none'}")
        if missing_jobs or missing_ai:
            click.echo("Schema is outdated. Run: flask --app main db upgrade")
        else:
            click.echo("Schema is compatible.")

    @app.cli.command("repair-local-db")
    def repair_local_db() -> None:
        """Idempotently add known missing columns to a local SQLite database."""
        from app.db.schema_compat import ensure_local_sqlite_schema_compatibility

        repaired = ensure_local_sqlite_schema_compatibility()
        if repaired:
            click.echo("Added columns: " + ", ".join(repaired))
            click.echo("Now run: flask --app main db upgrade")
        else:
            click.echo("No local SQLite compatibility repairs were required.")
