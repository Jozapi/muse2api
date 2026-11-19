"""The fork must track its own repository by default so an upgrade never drops its fixes."""
import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MUSE2API_KEY", "test-only")
sys.path.insert(0, str(ROOT))


@pytest.mark.parametrize("env, expected", [
    ({}, "Jozapi/muse2api"),
    ({"MUSE2API_REPO": "someone/else"}, "someone/else"),
    ({"MUSE2API_REPOSITORY": "czg86389-hub/muse2api", "MUSE2API_REPO": "someone/else"},
     "czg86389-hub/muse2api"),
])
def test_repo_slug(monkeypatch, env, expected):
    for key in ("MUSE2API_REPOSITORY", "MUSE2API_REPO"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import app
    try:
        app = importlib.reload(app)
        assert app.REPO_SLUG == expected
        assert app.REPO_URL == f"https://github.com/{expected}"
    finally:
        for key in ("MUSE2API_REPOSITORY", "MUSE2API_REPO"):
            monkeypatch.delenv(key, raising=False)
        importlib.reload(app)
