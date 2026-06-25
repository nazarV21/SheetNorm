"""automatic local model selection

Revision ID: 0004_auto_model_selection
Revises: 0003_work_sessions_and_memory_mode
Create Date: 2026-06-25
"""
from alembic import op
import sqlalchemy as sa


revision = "0004_auto_model_selection"
down_revision = "0003_work_sessions_and_memory_mode"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade():
    columns = _columns("ai_settings")
    with op.batch_alter_table("ai_settings") as batch:
        if "selection_mode" not in columns:
            batch.add_column(sa.Column("selection_mode", sa.String(length=32), nullable=False, server_default="auto"))
        if "auto_activate" not in columns:
            batch.add_column(sa.Column("auto_activate", sa.Boolean(), nullable=False, server_default=sa.true()))
        if "auto_test" not in columns:
            batch.add_column(sa.Column("auto_test", sa.Boolean(), nullable=False, server_default=sa.true()))
        if "reselect_if_unavailable" not in columns:
            batch.add_column(sa.Column("reselect_if_unavailable", sa.Boolean(), nullable=False, server_default=sa.true()))
        if "min_free_ram_gb" not in columns:
            batch.add_column(sa.Column("min_free_ram_gb", sa.Float(), nullable=False, server_default="2"))
        if "max_ram_usage_ratio" not in columns:
            batch.add_column(sa.Column("max_ram_usage_ratio", sa.Float(), nullable=False, server_default="0.72"))
        if "auto_selection_reason" not in columns:
            batch.add_column(sa.Column("auto_selection_reason", sa.Text(), nullable=False, server_default=""))
        if "hardware_fingerprint" not in columns:
            batch.add_column(sa.Column("hardware_fingerprint", sa.String(length=128), nullable=False, server_default=""))
        if "last_test_model_signature" not in columns:
            batch.add_column(sa.Column("last_test_model_signature", sa.String(length=256), nullable=False, server_default=""))


def downgrade():
    columns = _columns("ai_settings")
    with op.batch_alter_table("ai_settings") as batch:
        for name in (
            "last_test_model_signature",
            "hardware_fingerprint",
            "auto_selection_reason",
            "max_ram_usage_ratio",
            "min_free_ram_gb",
            "reselect_if_unavailable",
            "auto_test",
            "auto_activate",
            "selection_mode",
        ):
            if name in columns:
                batch.drop_column(name)
