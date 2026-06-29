#!/usr/bin/env python3
"""Tests for the SessionStart health-banner hook.

The hook must: stay silent when all-green, surface unhealthy/restarting
containers and down scrape targets when they exist, treat a wedged dockerd as a
(reported) signal rather than a crash, and never raise. We exercise the pure
helpers directly and stub `_run` so the suite needs no live docker/Prometheus.

Run: uv run pytest .claude/hooks
"""

import importlib.util
import io
import os
import types

_HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session-health.py")
_spec = importlib.util.spec_from_file_location("session_health", _HOOK)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _result(stdout):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)


# --- format_banner -----------------------------------------------------------


def test_banner_empty_when_no_problems():
    assert _mod.format_banner([]) == ""


def test_banner_lists_problems_and_triage():
    out = _mod.format_banner(["  ✗ jellyfin — unhealthy (x)"])
    assert "issues detected" in out
    assert "jellyfin" in out
    assert "triage" in out  # always points the reader at the probe commands


# --- docker_problems ---------------------------------------------------------


def test_docker_problems_parses_unhealthy_and_restarting(monkeypatch):
    calls = iter(
        [
            _result("jellyfin\tUp 2 hours (unhealthy)\n"),  # health=unhealthy filter
            _result(
                "sonarr\tRestarting (1) 3 seconds ago\n"
            ),  # status=restarting filter
        ]
    )
    monkeypatch.setattr(_mod, "_run", lambda *a, **k: next(calls))
    lines, ok = _mod.docker_problems()
    assert ok is True
    assert any("jellyfin" in l and "unhealthy" in l for l in lines)
    assert any("sonarr" in l and "restarting" in l for l in lines)


def test_docker_problems_all_green(monkeypatch):
    monkeypatch.setattr(_mod, "_run", lambda *a, **k: _result(""))
    lines, ok = _mod.docker_problems()
    assert lines == []
    assert ok is True


def test_docker_unreachable_is_reported_not_raised(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(_mod, "_run", boom)
    lines, ok = _mod.docker_problems()
    assert ok is False
    assert any("docker unreachable" in l for l in lines)


# --- target_problems ---------------------------------------------------------

_TARGETS_ONE_DOWN = (
    '{"data":{"activeTargets":['
    '{"health":"up","labels":{"job":"traefik","instance":"traefik:8080"}},'
    '{"health":"down","labels":{"job":"loki","instance":"loki:3100"},'
    '"lastError":"connection refused"}'
    "]}}"
)


def test_target_problems_flags_down(monkeypatch):
    monkeypatch.setattr(_mod, "_run", lambda *a, **k: _result(_TARGETS_ONE_DOWN))
    bad = _mod.target_problems()
    assert len(bad) == 1
    assert "loki" in bad[0] and "connection refused" in bad[0]


def test_target_problems_all_up(monkeypatch):
    up = '{"data":{"activeTargets":[{"health":"up","labels":{"job":"x"}}]}}'
    monkeypatch.setattr(_mod, "_run", lambda *a, **k: _result(up))
    assert _mod.target_problems() == []


def test_target_problems_swallows_bad_json(monkeypatch):
    monkeypatch.setattr(_mod, "_run", lambda *a, **k: _result("not json"))
    assert _mod.target_problems() == []  # monitoring hiccup must never blow up the hook


# --- main orchestration ------------------------------------------------------


def _run_main(monkeypatch, stdin, *, dock=None, ok=True, targets=None, env=None):
    monkeypatch.setattr(_mod.sys, "stdin", io.StringIO(stdin))
    monkeypatch.setattr(_mod, "docker_problems", lambda: (dock or [], ok))
    monkeypatch.setattr(_mod, "target_problems", lambda: targets or [])
    if env:
        for k, v in env.items():
            monkeypatch.setenv(k, v)


def test_main_silent_on_compact(monkeypatch, capsys):
    _run_main(
        monkeypatch,
        '{"source":"compact"}',
        dock=["  ✗ x — unhealthy (y)"],
        env={"SESSION_HEALTH_VERBOSE": "1"},
    )
    assert _mod.main() == 0
    assert capsys.readouterr().out == ""  # no re-banner mid-session


def test_main_silent_when_green(monkeypatch, capsys):
    _run_main(monkeypatch, '{"source":"startup"}')
    assert _mod.main() == 0
    assert capsys.readouterr().out == ""


def test_main_prints_banner_on_problem(monkeypatch, capsys):
    _run_main(
        monkeypatch, '{"source":"startup"}', dock=["  ✗ jellyfin — unhealthy (x)"]
    )
    assert _mod.main() == 0
    assert "jellyfin" in capsys.readouterr().out


def test_main_skips_targets_when_docker_down(monkeypatch, capsys):
    # docker_ok=False must short-circuit the Prometheus probe entirely.
    called = {"targets": False}

    def tp():
        called["targets"] = True
        return []

    monkeypatch.setattr(_mod.sys, "stdin", io.StringIO('{"source":"startup"}'))
    monkeypatch.setattr(
        _mod, "docker_problems", lambda: (["  ✗ docker unreachable"], False)
    )
    monkeypatch.setattr(_mod, "target_problems", tp)
    assert _mod.main() == 0
    assert called["targets"] is False
    assert "docker unreachable" in capsys.readouterr().out
