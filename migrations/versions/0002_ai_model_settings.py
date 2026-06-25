"""AI model settings

Revision ID: 0002_ai_model_settings
Revises: 0001_team_ready_schema
Create Date: 2026-06-25
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_ai_model_settings"
down_revision = "0001_team_ready_schema"
branch_labels = None
depends_on = None


def upgrade():
    if "ai_settings" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "ai_settings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("backend", sa.String(length=32), nullable=False, server_default="fallback"),
        sa.Column("selected_model_relative_path", sa.String(length=1024), nullable=True),
        sa.Column("active_model_relative_path", sa.String(length=1024), nullable=True),
        sa.Column("performance_profile", sa.String(length=32), nullable=False, server_default="balanced"),
        sa.Column("context_tokens", sa.Integer(), nullable=False, server_default="4096"),
        sa.Column("max_completion_tokens", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("n_threads", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("n_batch", sa.Integer(), nullable=False, server_default="128"),
        sa.Column("n_gpu_layers", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("temperature", sa.Float(), nullable=False, server_default="0.15"),
        sa.Column("last_test_status", sa.String(length=32), nullable=False, server_default="not_tested"),
        sa.Column("last_test_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("backend in ('fallback', 'llama_cpp')", name="ck_ai_settings_backend"),
        sa.CheckConstraint(
            "performance_profile in ('economy', 'balanced', 'performance', 'custom')",
            name="ck_ai_settings_profile",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    if "ai_settings" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("ai_settings")
