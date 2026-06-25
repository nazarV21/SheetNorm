"""assistant work sessions and AI memory mode

Revision ID: 0003_work_sessions_and_memory_mode
Revises: 0002_ai_model_settings
Create Date: 2026-06-25
"""
from alembic import op
import sqlalchemy as sa


revision = "0003_work_sessions_and_memory_mode"
down_revision = "0002_ai_model_settings"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade():
    job_columns = _columns("processing_jobs")
    with op.batch_alter_table("processing_jobs") as batch:
        if "job_kind" not in job_columns:
            batch.add_column(sa.Column("job_kind", sa.String(length=32), nullable=False, server_default="conversion"))
        if "selected_sheet" not in job_columns:
            batch.add_column(sa.Column("selected_sheet", sa.String(length=255), nullable=True))
        if "execution_mode" not in job_columns:
            batch.add_column(sa.Column("execution_mode", sa.String(length=32), nullable=True))
        if "resume_step" not in job_columns:
            batch.add_column(sa.Column("resume_step", sa.Integer(), nullable=False, server_default="1"))
        if "assistant_state" not in job_columns:
            batch.add_column(sa.Column("assistant_state", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))

    ai_columns = _columns("ai_settings")
    with op.batch_alter_table("ai_settings") as batch:
        if "memory_mode" not in ai_columns:
            batch.add_column(sa.Column("memory_mode", sa.String(length=32), nullable=False, server_default="economy"))
        if "idle_unload_seconds" not in ai_columns:
            batch.add_column(sa.Column("idle_unload_seconds", sa.Integer(), nullable=False, server_default="300"))


def downgrade():
    ai_columns = _columns("ai_settings")
    with op.batch_alter_table("ai_settings") as batch:
        if "idle_unload_seconds" in ai_columns:
            batch.drop_column("idle_unload_seconds")
        if "memory_mode" in ai_columns:
            batch.drop_column("memory_mode")

    job_columns = _columns("processing_jobs")
    with op.batch_alter_table("processing_jobs") as batch:
        for name in ("assistant_state", "resume_step", "execution_mode", "selected_sheet", "job_kind"):
            if name in job_columns:
                batch.drop_column(name)
