"""repair partially upgraded local schemas

Revision ID: 0005_schema_repair
Revises: 0004_auto_model_selection
Create Date: 2026-06-25
"""
from alembic import op
import sqlalchemy as sa


revision = "0005_schema_repair"
down_revision = "0004_auto_model_selection"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade():
    job_columns = _columns("processing_jobs")
    if job_columns:
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
    if ai_columns:
        with op.batch_alter_table("ai_settings") as batch:
            if "memory_mode" not in ai_columns:
                batch.add_column(sa.Column("memory_mode", sa.String(length=32), nullable=False, server_default="economy"))
            if "idle_unload_seconds" not in ai_columns:
                batch.add_column(sa.Column("idle_unload_seconds", sa.Integer(), nullable=False, server_default="300"))
            if "selection_mode" not in ai_columns:
                batch.add_column(sa.Column("selection_mode", sa.String(length=32), nullable=False, server_default="auto"))
            if "auto_activate" not in ai_columns:
                batch.add_column(sa.Column("auto_activate", sa.Boolean(), nullable=False, server_default=sa.true()))
            if "auto_test" not in ai_columns:
                batch.add_column(sa.Column("auto_test", sa.Boolean(), nullable=False, server_default=sa.true()))
            if "reselect_if_unavailable" not in ai_columns:
                batch.add_column(sa.Column("reselect_if_unavailable", sa.Boolean(), nullable=False, server_default=sa.true()))
            if "min_free_ram_gb" not in ai_columns:
                batch.add_column(sa.Column("min_free_ram_gb", sa.Float(), nullable=False, server_default="2"))
            if "max_ram_usage_ratio" not in ai_columns:
                batch.add_column(sa.Column("max_ram_usage_ratio", sa.Float(), nullable=False, server_default="0.72"))
            if "auto_selection_reason" not in ai_columns:
                batch.add_column(sa.Column("auto_selection_reason", sa.Text(), nullable=False, server_default=""))
            if "hardware_fingerprint" not in ai_columns:
                batch.add_column(sa.Column("hardware_fingerprint", sa.String(length=128), nullable=False, server_default=""))
            if "last_test_model_signature" not in ai_columns:
                batch.add_column(sa.Column("last_test_model_signature", sa.String(length=256), nullable=False, server_default=""))


def downgrade():
    # Repair migrations are intentionally non-destructive.
    pass
