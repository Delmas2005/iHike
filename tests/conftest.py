"""Test set-up: no network, no keys, no models.

`ihike.config` reads the environment at import time, and the community store lives in
folders relative to the working directory, so both are set here before any test imports iHike.
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
os.chdir(_WORKDIR)  # data/ and pending_photos/ are relative to the working directory

import requests  # noqa: E402

from fakes import FakeWeb  # noqa: E402

from ihike import community, geo  # noqa: E402


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
