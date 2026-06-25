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

