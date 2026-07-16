"""Behavioural tests for host_lib — the shared I/O shell for gitops_deploy.py / renovate_notify.py.

host_lib is importable (stdlib only, no module-level config read), so its invariants are tested here
directly. This is the behavioural home of the Discord User-Agent + 2xx-only contract that gitops's
un-importable discord() previously pinned via AST guards (test_gitops_discord_contract.py).
"""

import json
from unittest import mock

import host_lib


def test_parse_env_file_skips_comments_and_splits_on_first_equals(tmp_path):
    p = tmp_path / "config.env"
    p.write_text("# a comment\nA=1\nB=x=y\n\n  \nNOEQUALS\n")
    assert host_lib.parse_env_file(str(p)) == {"A": "1", "B": "x=y"}


def test_atomic_write_creates_dirs_replaces_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "sub" / "state"
    host_lib.atomic_write(str(p), "hello")
    assert p.read_text() == "hello"
    host_lib.atomic_write(str(p), "world")  # overwrite
    assert p.read_text() == "world"
    assert not (tmp_path / "sub" / "state.tmp").exists()


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_discord_post_empty_webhook_is_false_and_logs():
    logs = []
    assert host_lib.discord_post("", "hi", "ua", log=logs.append) is False
    assert logs  # a skip reason was logged


def test_discord_post_sends_user_agent_and_true_on_2xx():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _Resp(204)

    with mock.patch("host_lib.urllib.request.urlopen", fake_urlopen):
        ok = host_lib.discord_post("https://example/webhook", "hello", "gitops-deploy")
    assert ok is True
    req = captured["req"]
    # urllib capitalises header keys ("User-agent"); assert on the value to stay robust.
    assert "gitops-deploy" in req.headers.values()
    assert json.loads(req.data)["content"] == "hello"


def test_discord_post_false_on_non_2xx():
    with mock.patch(
        "host_lib.urllib.request.urlopen", lambda req, timeout=None: _Resp(500)
    ):
        assert host_lib.discord_post("https://x", "hi", "ua") is False


def test_discord_post_false_and_logs_on_exception():
    def boom(req, timeout=None):
        raise OSError("network down")

    logs = []
    with mock.patch("host_lib.urllib.request.urlopen", boom):
        ok = host_lib.discord_post("https://x", "hi", "ua", log=logs.append)
    assert ok is False
    assert logs  # the failure was logged, not raised
