"""Shared pytest configuration and fixtures.

Environment variables must be set before ``app.main`` is imported because
the database engine and security settings are initialised at import time.
"""

import os

# Use an isolated SQLite database so tests never touch the dev/prod database.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("MAIL_USERNAME", "")
os.environ.setdefault("MAIL_PASSWORD", "")
os.environ.setdefault("MAIL_FROM", "")

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app)
