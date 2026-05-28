"""Unit tests for the GitHub device-flow login helper.

Live OAuth flow itself is not exercised — those need github.com. We test
the file-mutation logic (which is the bit most likely to mangle a real
``.env``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load the script as a module (it lives outside the app package).
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gh_device_login.py"
_spec = importlib.util.spec_from_file_location("gh_device_login", _SCRIPT)
assert _spec and _spec.loader
gh_device_login = importlib.util.module_from_spec(_spec)
sys.modules["gh_device_login"] = gh_device_login
_spec.loader.exec_module(gh_device_login)


def test_upsert_creates_env_when_missing(tmp_path):
    env = tmp_path / ".env"
    gh_device_login.upsert_env_token(env, "ghp_abc123")
    assert env.read_text() == "GITHUB_TOKEN=ghp_abc123\n"


def test_upsert_appends_when_key_absent(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MERAKI_API_KEY=xyz\nDEBUG=true\n")
    gh_device_login.upsert_env_token(env, "ghp_newone")
    text = env.read_text()
    assert "MERAKI_API_KEY=xyz" in text
    assert "DEBUG=true" in text
    assert text.endswith("GITHUB_TOKEN=ghp_newone\n")


def test_upsert_replaces_existing_key_inline(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "MERAKI_API_KEY=xyz\nGITHUB_TOKEN=old_placeholder\nMODEL_ID=openai/gpt-4o-mini\n"
    )
    gh_device_login.upsert_env_token(env, "ghp_freshtoken")
    text = env.read_text()
    assert "GITHUB_TOKEN=ghp_freshtoken" in text
    assert "GITHUB_TOKEN=old_placeholder" not in text
    # Other keys preserved + order kept.
    lines = text.strip().splitlines()
    assert lines[0] == "MERAKI_API_KEY=xyz"
    assert lines[1] == "GITHUB_TOKEN=ghp_freshtoken"
    assert lines[2] == "MODEL_ID=openai/gpt-4o-mini"


def test_upsert_handles_no_trailing_newline(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DEBUG=true")  # no trailing newline
    gh_device_login.upsert_env_token(env, "ghp_xyz")
    text = env.read_text()
    assert text.endswith("\n")
    assert "DEBUG=true" in text
    assert "GITHUB_TOKEN=ghp_xyz" in text


def test_upsert_only_replaces_exact_key(tmp_path):
    """Don't accidentally replace GITHUB_TOKEN_OLD or similar."""
    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN_BACKUP=keep_me\nGITHUB_TOKEN=replace_me\n")
    gh_device_login.upsert_env_token(env, "ghp_new")
    text = env.read_text()
    assert "GITHUB_TOKEN_BACKUP=keep_me" in text
    assert "GITHUB_TOKEN=ghp_new" in text
    assert "replace_me" not in text


def test_poll_handles_slow_down(monkeypatch):
    """A 'slow_down' response should back off, not crash."""

    class _Resp:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    responses = iter(
        [
            _Resp({"error": "authorization_pending"}),
            _Resp({"error": "slow_down", "interval": 1}),
            _Resp({"access_token": "ghp_finally"}),
        ]
    )

    def fake_post(*_a, **_k):
        return next(responses)

    monkeypatch.setattr(gh_device_login.httpx, "post", fake_post)
    monkeypatch.setattr(gh_device_login.time, "sleep", lambda _s: None)

    token = gh_device_login.poll_for_token("cid", "dev", interval=1, expires_in=30)
    assert token == "ghp_finally"


def test_poll_raises_on_access_denied(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"error": "access_denied", "error_description": "nope"}

    monkeypatch.setattr(gh_device_login.httpx, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr(gh_device_login.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError, match="access_denied"):
        gh_device_login.poll_for_token("cid", "dev", interval=1, expires_in=30)
