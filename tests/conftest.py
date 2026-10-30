"""Test set-up: no network, no keys, no models.

`ihike.config` reads the environment at import time, so it is set here before any test
imports iHike. The community store is redirected to a temporary folder.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

os.environ["ORS_API_KEY"] = "test-key"  # pragma: allowlist secret - a dummy value, never sent anywhere
for name in ("HF_TOKEN", "HIKES_DATASET"):
    os.environ.pop(name, None)

import requests  # noqa: E402

from fakes import FakeWeb  # noqa: E402
from ihike import community, geo  # noqa: E402

_STORE = Path(tempfile.mkdtemp(prefix="ihike-tests-"))
community.DATA_DIR = _STORE / "data"
community.HIKES_FILE = community.DATA_DIR / "hikes.jsonl"
community.PHOTOS_DIR = community.DATA_DIR / "photos"
community.PENDING_DIR = _STORE / "pending_photos"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any request a test did not set up fails loudly instead of reaching the internet."""

    def refuse(*args, **kwargs):
        raise AssertionError(f"unexpected network call: {args[:1]}")

    monkeypatch.setattr(requests, "get", refuse)
    monkeypatch.setattr(requests, "post", refuse)


@pytest.fixture
def web(monkeypatch):
    fake = FakeWeb(geo.haversine_km)
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


@pytest.fixture
def store():
    """An empty community store for the test."""
    community.HIKES_FILE.unlink(missing_ok=True)
    for folder in (community.PHOTOS_DIR, community.PENDING_DIR):
        shutil.rmtree(folder, ignore_errors=True)
    community.ensure_folders()
    return community


@pytest.fixture
def client(web, store):
    from fastapi.testclient import TestClient

    from ihike.web import app

    return TestClient(app)
