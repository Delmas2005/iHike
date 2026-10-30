"""Importing iHike has no side effects; the community store is set up when the server starts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from ihike import community
from ihike.web import app

ROOT = Path(__file__).resolve().parents[2]


def test_importing_the_server_creates_no_folders(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("HF_TOKEN", "HIKES_DATASET")}
    env["PYTHONPATH"] = str(ROOT)
    subprocess.run([sys.executable, "-c", "import ihike.web"], cwd=tmp_path, env=env, check=True, timeout=60)
    assert list(tmp_path.iterdir()) == []


def test_server_start_up_prepares_the_store(monkeypatch, web):
    calls = []
    monkeypatch.setattr(community, "init_storage", lambda: calls.append(1))
    with TestClient(app) as client:
        assert client.get("/api/config").status_code == 200
    assert calls == [1]


def test_without_a_dataset_storage_is_local_only(tmp_path, monkeypatch):
    monkeypatch.setattr(community, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(community, "PHOTOS_DIR", tmp_path / "data" / "photos")
    monkeypatch.setattr(community, "PENDING_DIR", tmp_path / "pending")
    community.init_storage()
    assert community.scheduler is None
    assert (tmp_path / "data" / "photos").is_dir() and (tmp_path / "pending").is_dir()
