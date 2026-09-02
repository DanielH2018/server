#!/usr/bin/env python3
"""Tests for deploy_detach_notify.py -- the `scripts/deploy.sh --detach` completion notifier.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_detach_notify.py
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
                1, "config: not found, and not a declared service on any host (...)"
            )
        return _result(1, "config: no Deployment or DaemonSet in this namespace (...)")

    state, detail = notify_mod.check_one("config", run=run)
    assert state == "skipped"


def test_check_one_skips_a_role_that_declares_no_workload():
    """netpol-baseline and media-volume render NetworkPolicies and PVCs, never a workload.
    Nothing to gate, so the tag is skipped rather than failed."""

    def run(argv, **kwargs):
        if "--docker" in argv:
            return _result(
                1, "netpol-baseline: not found, and not a declared service on any host"
            )
        return _result(
            1,
            "netpol-baseline: the role declares no rollout-checkable workload "
            "(no Deployment, DaemonSet or StatefulSet in its manifests)",
        )

    state, _ = notify_mod.check_one("netpol-baseline", run=run)
    assert state == "skipped"


def test_check_one_flags_a_declared_pi_container_that_is_absent():
    """The reject half of the skip above, and the safety-critical half of the 2026-09-01 fix.
    A Pi service daniel-pi's inventory declares, with no container on the host, is a deploy
    that failed. It shared the undeclared case's "not found (not created" message until then,
    so it reported `skipped` and the verdict stayed `settled`."""

    def run(argv, **kwargs):
        if "--docker" in argv:
            return _result(
                1,
                "wg-easy: MISSING — daniel-pi's inventory declares this service and the host "
                "has no such container, so the deploy did not create it",
            )
        return _result(1, "wg-easy: no Deployment or DaemonSet in this namespace (...)")

    state, detail = notify_mod.check_one("wg-easy", run=run)
    assert state == "unhealthy"
    assert "MISSING" in detail


def test_check_one_flags_a_resolved_k8s_workload_that_is_absent():
    """claude-otel deploys six workloads and none is named claude-otel, so the old gate asked
    for a name nothing carries, got "no Deployment or DaemonSet", and skipped. Now the role's
    manifests name the workloads, and one of them missing is a failed deploy."""

    def run(argv, **kwargs):
        return _result(
            1,
            "claude-otel: 1 of 6 workloads FAILED the gate — Deployment "
            "observability/grafana",
        )

    state, detail = notify_mod.check_one("claude-otel", run=run)
    assert state == "unhealthy"
    assert "FAILED the gate" in detail


def test_gate_fails_on_a_resolved_workload_that_is_absent():
    """The whole point, at the verdict level: this used to come back settled."""

    def run(argv, **kwargs):
        return _result(
            1,
            "claude-otel: 1 of 6 workloads FAILED the gate — Deployment homelab/grafana",
        )

    ok, lines = notify_mod.gate(["claude-otel"], ansible_ok=True, run=run)
    assert ok is False
    assert not any("skipped" in line for line in lines)


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
            return _result(
                1, "config: not found, and not a declared service on any host"
            )
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


def test_no_post_prints_the_verdict_without_notifying(monkeypatch, capsys, tmp_path):
    """land.sh reuses this verdict but returns it to the session, not to Discord.
    Posting from both paths would split one verdict across two channels."""
    posted = []
    monkeypatch.setattr(notify_mod, "notify", lambda c: posted.append(c))
    monkeypatch.setattr(notify_mod, "gate", lambda tags, ok: (True, ["sonarr: ok"]))
    log = tmp_path / "deploy.log"
    log.write_text("")
    rc = notify_mod.main(
        ["--status", "0", "--log", str(log), "--tags", "sonarr", "--no-post"]
    )
    assert rc == 0
    assert posted == [], "--no-post must not reach Discord"
    assert "sonarr: ok" in capsys.readouterr().out


def test_without_no_post_the_verdict_is_notified(monkeypatch, capsys, tmp_path):
    """The reject half. Without it a --no-post that silently disabled ALL posting would
    pass the test above, and every automated deploy outcome would stop reporting."""
    posted = []
    monkeypatch.setattr(notify_mod, "notify", lambda c: posted.append(c))
    monkeypatch.setattr(notify_mod, "gate", lambda tags, ok: (True, ["sonarr: ok"]))
    log = tmp_path / "deploy.log"
    log.write_text("")
    notify_mod.main(["--status", "0", "--log", str(log), "--tags", "sonarr"])
    assert len(posted) == 1


def test_no_post_still_reports_an_unhealthy_verdict(monkeypatch, tmp_path):
    """The flag changes the destination, never the verdict."""
    monkeypatch.setattr(notify_mod, "notify", lambda c: None)
    monkeypatch.setattr(notify_mod, "gate", lambda tags, ok: (False, ["sonarr: down"]))
    log = tmp_path / "deploy.log"
    log.write_text("")
    rc = notify_mod.main(
        ["--status", "0", "--log", str(log), "--tags", "sonarr", "--no-post"]
    )
    assert rc == 1


#
# The two modules below are joined by substring matching across a subprocess boundary, so
# nothing but these tests connects probe.py's wording to the verdict it decides. The cases
# above use hand-written strings; these take the REAL messages from probe_health and assert
# each lands on the side of NOT_APPLICABLE_MARKERS it is meant to. A reword that put a failed
# deploy back on the skip path would leave every test above green.
#
from datetime import datetime, timezone  # noqa: E402

import probe_health  # noqa: E402 — resolved by pyproject's pythonpath, alongside notify_mod

_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _matches_a_marker(message):
    return any(marker in message for marker in notify_mod.NOT_APPLICABLE_MARKERS)


def test_probes_absent_workload_messages_carry_no_skip_marker():
    """The safety-critical assertion in this file. Both messages mean "the thing that should
    exist is gone", which must fail the verdict rather than skip it."""
    docker_missing, _ = probe_health.format_health([], "wg-easy", declared=True)
    k8s_missing, _ = probe_health.format_role_health(
        "claude-otel",
        [("observability", "Deployment", "grafana", None, None)],
        _NOW,
    )
    for message in (docker_missing, k8s_missing.splitlines()[0]):
        assert not _matches_a_marker(message), message


def test_probes_not_applicable_messages_do_carry_a_skip_marker():
    """The other half: a tag that names nothing must still skip, or every block tag in a
    --tags list turns an otherwise good deploy red."""
    undeclared, _ = probe_health.format_health([], "config", declared=False)
    no_workload, _ = probe_health.format_k8s_health(None, None, "config", _NOW)
    for message in (undeclared, no_workload):
        assert _matches_a_marker(message), message


def test_the_role_declares_nothing_message_carries_a_skip_marker():
    """run_health builds this one inline rather than through a formatter, so it is asserted
    against the source text — the marker and the message must not drift apart."""
    source = (
        probe_health.REPO / "scripts" / "diagnostics" / "probe_health.py"
    ).read_text()
    assert "the role declares no rollout-checkable workload" in source
    assert _matches_a_marker("x: the role declares no rollout-checkable workload (...)")
