from __future__ import annotations

from pathlib import Path

import pytest

from app import create_app
from config import TestConfig


@pytest.fixture()
def app(tmp_path: Path):
    application = create_app(TestConfig)
    application.config.update(
        INPUT_DIR=tmp_path / "input",
        OUTPUT_DIR=tmp_path / "output",
        HISTORY_FILE=tmp_path / "history.json",
        RULES_FILE=tmp_path / "rules.json",
        TRAINING_EXAMPLES_FILE=tmp_path / "training_examples.json",
        TRAINING_EXAMPLES_DIR=tmp_path / "training_examples",
        AI_BACKEND="fallback",
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()
