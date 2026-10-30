"""Test set-up: no network, no keys, no models.

The engine reads its configuration and creates its data folders at import time, so the
environment and the working directory are set here, before any test imports it.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ["ORS_API_KEY"] = "test-key"
for name in ("HF_TOKEN", "HIKES_DATASET"):
    os.environ.pop(name, None)
_WORKDIR = tempfile.mkdtemp(prefix="ihike-tests-")
os.chdir(_WORKDIR)  # data/ and pending_photos/ are created relative to the working directory

import requests  # noqa: E402

import engine as _engine  # noqa: E402
from fakes import FakeWeb  # noqa: E402


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any request a test did not set up fails loudly instead of reaching the internet."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"unexpected network call: {args[:1]}")

    monkeypatch.setattr(requests, "get", refuse)
    monkeypatch.setattr(requests, "post", refuse)


@pytest.fixture
def engine():
    return _engine


@pytest.fixture
def web(monkeypatch):
    fake = FakeWeb(_engine.haversine_km)
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


@pytest.fixture
def store():
    """An empty community store for the test."""
    _engine.HIKES_FILE.unlink(missing_ok=True)
    for folder in (_engine.PHOTOS_DIR, _engine.PENDING_DIR):
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
    return _engine


@pytest.fixture
def client(web, store):
    from fastapi.testclient import TestClient

    import app as server

    return TestClient(server.app)
