#!/usr/bin/env python3
"""Tests for deploy_detach_notify.py -- the `scripts/deploy.sh --detach` completion notifier.

Run: uv run pytest scripts/deploy_tools/test_deploy_detach_notify.py
"""

from __future__ import annotations

import types


import deploy_detach_notify as notify_mod


def _result(returncode, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_check_one_ok_on_k8s_first_try():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return _result(0, "jellyfin: 1/1 ready, 1 updated, restarts=0")

    state, detail = notify_mod.check_one("jellyfin", run=run)
    assert state == "ok"
    assert "jellyfin" in detail
    assert len(calls) == 1  # no --docker fallback needed
    assert "--docker" not in calls[0]


def test_check_one_falls_back_to_docker(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "--docker" in argv:
            return _result(0, "wg-easy: running, health=healthy, restarts=0")
        return _result(1, "wg-easy: no Deployment or DaemonSet in this namespace (...)")

    state, detail = notify_mod.check_one("wg-easy", run=run)
    assert state == "ok"
    assert "wg-easy" in detail
    assert len(calls) == 2
    assert "--docker" in calls[1]


def test_check_one_skipped_when_neither_platform_recognizes_it():
    def run(argv, **kwargs):
        if "--docker" in argv:
            return _result(
                1, "config: not found (not created — wrong name, or deploy failed?)"
            )
        return _result(1, "config: no Deployment or DaemonSet in this namespace (...)")

    state, detail = notify_mod.check_one("config", run=run)
    assert state == "skipped"


def test_check_one_unhealthy_is_not_confused_with_not_applicable():
    def run(argv, **kwargs):
        return _result(
            1, "sonarr: 0/1 ready, 0 updated, restarts=4 — RECENT RESTART: ..."
        )

    state, detail = notify_mod.check_one("sonarr", run=run)
    assert state == "unhealthy"
    assert "sonarr" in detail


def test_check_one_survives_a_timeout():
    def run(argv, **kwargs):
        raise __import__("subprocess").TimeoutExpired(argv, kwargs.get("timeout", 30))

    state, detail = notify_mod.check_one("jellyfin", run=run)
    assert state == "unhealthy"
    assert "timed out" in detail


def test_gate_fails_fast_on_ansible_failure_without_health_checking():
    called = {"n": 0}

    def run(argv, **kwargs):
        called["n"] += 1
        return _result(0, "jellyfin: ok")

    ok, lines = notify_mod.gate(["jellyfin"], ansible_ok=False, run=run)
    assert ok is False
    assert called["n"] == 0
    assert any("ansible-playbook exited non-zero" in line for line in lines)


def test_gate_settled_when_all_tags_healthy():
    def run(argv, **kwargs):
        return _result(0, f"{argv[5]}: 1/1 ready, restarts=0")

    ok, lines = notify_mod.gate(["jellyfin", "sonarr"], ansible_ok=True, run=run)
    assert ok is True
    assert len(lines) == 2


def test_gate_unsettled_when_one_tag_is_unhealthy():
    def run(argv, **kwargs):
        tag = argv[5]
        if tag == "sonarr":
            return _result(1, "sonarr: 0/1 ready — rollout incomplete")
        return _result(0, f"{tag}: 1/1 ready, restarts=0")

    ok, lines = notify_mod.gate(["jellyfin", "sonarr"], ansible_ok=True, run=run)
    assert ok is False


def test_gate_skipped_tag_does_not_fail_the_verdict():
    def run(argv, **kwargs):
        if "--docker" in argv:
            return _result(1, "config: not found (not created — wrong name?)")
        return _result(1, "config: no Deployment or DaemonSet in this namespace")

    ok, lines = notify_mod.gate(["config"], ansible_ok=True, run=run)
    assert ok is True
    assert any("skipped" in line for line in lines)


def test_gate_degrades_gracefully_with_no_tags():
    ok, lines = notify_mod.gate([], ansible_ok=True)
    assert ok is True
    assert any("no --tags given" in line for line in lines)


def test_notify_skips_silently_without_webhook_config(monkeypatch, capsys):
    monkeypatch.setattr(
        notify_mod, "HOST_LIB_PATH", notify_mod.REPO / "nonexistent-host-lib.py"
    )
    notify_mod.notify("some content")
    out = capsys.readouterr().out
    assert "skipping Discord notify" in out


def test_notify_never_raises_on_a_broken_host_lib(monkeypatch, tmp_path, capsys):
    # host_lib.py exists but is broken -- notify() must swallow the import/exec error rather
    # than crash the backgrounded deploy run that's waiting to append its own log lines after.
    broken = tmp_path / "host_lib.py"
    broken.write_text("raise RuntimeError('boom')\n")
    config = tmp_path / "config.env"
    config.write_text("DISCORD_WEBHOOK=https://example.invalid/webhook\n")
    monkeypatch.setattr(notify_mod, "HOST_LIB_PATH", broken)
    monkeypatch.setattr(notify_mod, "CONFIG_ENV_PATH", config)
    # notify() does a bare `from host_lib import ...` after putting HOST_LIB_PATH's dir on
    # sys.path, so a host_lib another test already imported is served from sys.modules and
    # this broken one never executes. Without the evict, the test passes alone and asserts
    # the wrong branch in suite order.
    monkeypatch.delitem(notify_mod.sys.modules, "host_lib", raising=False)
    notify_mod.notify("some content")  # must not raise
    out = capsys.readouterr().out
    assert "notify failed" in out


def test_main_returns_nonzero_on_unsettled_deploy(monkeypatch, capsys):
    monkeypatch.setattr(
        notify_mod, "gate", lambda tags, ok: (False, ["sonarr: unhealthy"])
    )
    monkeypatch.setattr(notify_mod, "notify", lambda content: None)
    code = notify_mod.main(["--status", "0", "--log", "/tmp/x.log", "--tags", "sonarr"])
    assert code == 1
    assert "FAILED" in capsys.readouterr().out


def test_main_returns_zero_on_settled_deploy(monkeypatch, capsys):
    monkeypatch.setattr(
        notify_mod, "gate", lambda tags, ok: (True, ["jellyfin: 1/1 ready"])
    )
    monkeypatch.setattr(notify_mod, "notify", lambda content: None)
    code = notify_mod.main(
        ["--status", "0", "--log", "/tmp/x.log", "--tags", "jellyfin"]
    )
    assert code == 0
    assert "settled" in capsys.readouterr().out


def test_main_parses_empty_tags_to_empty_list(monkeypatch):
    captured = {}

    def fake_gate(tags, ok):
        captured["tags"] = tags
        return True, []

    monkeypatch.setattr(notify_mod, "gate", fake_gate)
    monkeypatch.setattr(notify_mod, "notify", lambda content: None)
    notify_mod.main(["--status", "0", "--log", "/tmp/x.log"])
    assert captured["tags"] == []
