from __future__ import annotations

from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

try:
    from flask_migrate import Migrate
except ImportError:  # pragma: no cover - exercised when dependency is absent locally
    Migrate = None  # type: ignore[assignment]


db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate() if Migrate else None

