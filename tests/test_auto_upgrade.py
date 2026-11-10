"""Background silent auto-upgrade must stay opt-in via MUSE2API_AUTO_UPGRADE=1."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MUSE2API_KEY", "test-only")
sys.path.insert(0, str(ROOT))

import app


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


LOCAL_VERSION = "1.6.9"
LOCAL_SHA = "abc1234"
REMOTE_SHA = "deadbee"


def _fake_get(url, **kwargs):
    if "raw.githubusercontent.com" in url:
        return FakeResponse(200, {"version": LOCAL_VERSION, "highlights": [], "highlights_en": []})
    if "api.github.com" in url:
        commits = [{
            "sha": REMOTE_SHA + "f00dcafe",
            "commit": {"message": "feat: something new", "committer": {"date": "2026-11-10T00:00:00Z"}},
            "html_url": "https://github.com/example/example/commit/" + REMOTE_SHA,
        }]
        return FakeResponse(200, commits)
    raise AssertionError(f"unexpected URL requested in test: {url}")


@pytest.fixture
def repo_env(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(app.CFG, "base_dir", str(tmp_path))
    (tmp_path / "version.json").write_text(
        json.dumps({"version": LOCAL_VERSION, "highlights": [], "highlights_en": []}),
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".installed_sha").write_text(LOCAL_SHA, encoding="utf-8")
    app._UPDATE_CACHE["ts"] = 0.0
    app._UPDATE_CACHE["data"] = None
    yield
    app._UPDATE_CACHE["ts"] = 0.0
    app._UPDATE_CACHE["data"] = None


@pytest.mark.parametrize("raw_value", [None, "", "0", "nope"])
def test_auto_upgrade_defaults_off(repo_env, monkeypatch, raw_value):
    if raw_value is None:
        monkeypatch.delenv("MUSE2API_AUTO_UPGRADE", raising=False)
    else:
        monkeypatch.setenv("MUSE2API_AUTO_UPGRADE", raw_value)

    with patch("requests.get", side_effect=_fake_get), \
         patch.object(app.threading, "Thread") as thread_cls, \
         patch.object(app, "_silent_upgrade_worker") as worker, \
         patch.object(app, "_upgrade_from_github_sync") as upgrade:
        result = app._check_update_sync(force=True)

    thread_cls.assert_not_called()
    worker.assert_not_called()
    upgrade.assert_not_called()

    assert result["has_update"] is True
    assert result["local_sha"] == LOCAL_SHA
    assert result["remote_sha"] != result["local_sha"]
    assert result["up_to_date"] is False


def test_auto_upgrade_opt_in_triggers_silent_worker(repo_env, monkeypatch):
    monkeypatch.setenv("MUSE2API_AUTO_UPGRADE", "1")

    with patch("requests.get", side_effect=_fake_get), \
         patch.object(app.threading, "Thread") as thread_cls, \
         patch.object(app, "_silent_upgrade_worker") as worker:
        result = app._check_update_sync(force=True)

    thread_cls.assert_called_once()
    _, kwargs = thread_cls.call_args
    assert kwargs.get("target") is worker
    worker.assert_not_called()  # the thread itself is mocked out, so .start() never runs the target

    # External callers must still be told everything is up to date.
    assert result["has_update"] is False
    assert result["up_to_date"] is True
    assert result["local_sha"] == result["remote_sha"]


def test_check_update_sync_explicit_false_never_upgrades(repo_env, monkeypatch):
    monkeypatch.setenv("MUSE2API_AUTO_UPGRADE", "1")

    with patch("requests.get", side_effect=_fake_get), \
         patch.object(app.threading, "Thread") as thread_cls:
        result = app._check_update_sync(force=True, _auto_upgrade=False)

    thread_cls.assert_not_called()
    assert result["has_update"] is True


def test_admin_html_only_posts_upgrade_from_upgrade_now():
    html = (ROOT / "admin.html").read_text(encoding="utf-8")
    start = html.index("async function upgradeNow()")
    end = html.index("\n}", start)
    upgrade_now_body = html[start:end]

    occurrences = html.count("/admin/update/upgrade")
    assert occurrences == upgrade_now_body.count("/admin/update/upgrade")
    assert occurrences >= 1
