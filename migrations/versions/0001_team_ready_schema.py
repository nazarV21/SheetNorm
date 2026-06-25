"""team ready schema

Revision ID: 0001_team_ready_schema
Revises:
Create Date: 2026-06-25
"""
from alembic import op


revision = "0001_team_ready_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    from app.extensions import db
    import app.db.models  # noqa: F401

    db.metadata.create_all(bind=op.get_bind())


def downgrade():
    from app.extensions import db
    import app.db.models  # noqa: F401

    db.metadata.drop_all(bind=op.get_bind())

