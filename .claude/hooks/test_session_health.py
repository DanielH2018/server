#!/usr/bin/env python3
"""Tests for the SessionStart health-banner hook.

The hook must: stay silent when all-green, surface unhealthy/restarting
containers and down scrape targets when they exist, treat a wedged dockerd as a
(reported) signal rather than a crash, treat a host with no docker binary as
expected rather than broken, and never raise. We exercise the pure helpers
directly and stub `_run` so the suite needs no live docker/Prometheus.

Run: uv run pytest .claude/hooks
"""

import importlib.util
import io
import os
import subprocess
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


def test_wedged_dockerd_is_reported_not_raised(monkeypatch):
    """A docker that hangs is the signal the banner exists for."""

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("docker", 5)

    monkeypatch.setattr(_mod, "_run", boom)
    lines, ok = _mod.docker_problems()
    assert ok is False
    assert any("docker unreachable" in l for l in lines)


def test_missing_docker_binary_is_silent(monkeypatch):
    """daniel-box runs k3s with has_docker: false — no binary is expected, not broken.

    Warning here would put a false '✗ docker unreachable' on every session open on that
    host, forever, which is exactly the context noise the all-green contract avoids.
    """

    def boom(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(_mod, "_run", boom)
    lines, ok = _mod.docker_problems()
    assert lines == []
    # Still False: this only gates the docker section of the banner now — the Prometheus
    # check does not depend on docker and runs regardless (see test_main_runs_targets_
    # even_when_docker_down).
    assert ok is False


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


def test_is_scaled_to_zero_true_when_replicas_zero(monkeypatch):
    monkeypatch.setattr(_mod, "_run", lambda *a, **k: _result("0"))
    assert _mod._is_scaled_to_zero("terraria-stats", "homelab") is True


def test_is_scaled_to_zero_false_when_replicas_nonzero(monkeypatch):
    monkeypatch.setattr(_mod, "_run", lambda *a, **k: _result("1"))
    assert _mod._is_scaled_to_zero("loki", "homelab") is False


def test_is_scaled_to_zero_false_on_kubectl_failure(monkeypatch):
    # An unreadable answer must not be treated as "confirmed intentional" — a real problem
    # we can't explain has to stay visible, same fail-open-to-visible rule probe.py's
    # format_k8s_health uses for an unreadable restart time.
    monkeypatch.setattr(
        _mod,
        "_run",
        lambda *a, **k: types.SimpleNamespace(stdout="", stderr="x", returncode=1),
    )
    assert _mod._is_scaled_to_zero("sonarr", "homelab") is False


def test_is_scaled_to_zero_false_on_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("kubectl", 5)

    monkeypatch.setattr(_mod, "_run", boom)
    assert _mod._is_scaled_to_zero("sonarr", "homelab") is False


def test_is_scaled_to_zero_false_without_a_namespace():
    assert _mod._is_scaled_to_zero("sonarr", None) is False


_TARGETS_ONE_SCALED_TO_ZERO = (
    '{"data":{"activeTargets":['
    '{"health":"down","labels":{"job":"terraria-stats","instance":"terraria-stats:9420"},'
    '"lastError":"connection refused"},'
    '{"health":"down","labels":{"job":"loki","instance":"loki:3100"},'
    '"lastError":"connection refused"}'
    "]}}"
)


def test_target_problems_filters_scaled_to_zero_deployments(monkeypatch):
    # terraria-stats/valheim-stats are on-demand game servers deliberately scaled to 0 —
    # reporting them every session open forever is exactly the noise the all-green
    # contract exists to avoid, so only the genuinely-unexplained loki target survives.
    monkeypatch.setattr(_mod, "_k8s_namespace", lambda: "homelab")
    monkeypatch.setattr(
        _mod,
        "_is_scaled_to_zero",
        lambda job, ns: job == "terraria-stats",
    )
    monkeypatch.setattr(
        _mod, "_run", lambda *a, **k: _result(_TARGETS_ONE_SCALED_TO_ZERO)
    )
    bad = _mod.target_problems()
    assert len(bad) == 1
    assert "loki" in bad[0]
    assert not any("terraria-stats" in line for line in bad)


# --- master_moved_problems ----------------------------------------------------


def test_master_moved_silent_when_current(monkeypatch):
    monkeypatch.setattr(_mod, "_run", lambda *a, **k: _result("0\n"))
    assert _mod.master_moved_problems() == []


def test_master_moved_reports_commit_count(monkeypatch):
    monkeypatch.setattr(_mod, "_run", lambda *a, **k: _result("3\n"))
    lines = _mod.master_moved_problems()
    assert len(lines) == 1
    assert "3 commits behind origin/master" in lines[0]


def test_master_moved_singular_commit(monkeypatch):
    monkeypatch.setattr(_mod, "_run", lambda *a, **k: _result("1\n"))
    lines = _mod.master_moved_problems()
    assert "1 commit behind" in lines[0]  # not "1 commits"


def test_master_moved_never_fetches(monkeypatch):
    # The whole point of a read-only, always-runs SessionStart hook: this must be answerable
    # from the local object store alone, never a network call.
    seen = []
    monkeypatch.setattr(
        _mod, "_run", lambda cmd, *a, **k: seen.append(cmd) or _result("0\n")
    )
    _mod.master_moved_problems()
    assert seen and "fetch" not in seen[0]


def test_master_moved_silent_on_nonzero_git_exit(monkeypatch):
    # e.g. no origin/master ref in this checkout at all -- not this hook's job to diagnose.
    monkeypatch.setattr(
        _mod,
        "_run",
        lambda *a, **k: types.SimpleNamespace(stdout="", stderr="x", returncode=128),
    )
    assert _mod.master_moved_problems() == []


def test_master_moved_silent_on_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(_mod, "_run", boom)
    assert _mod.master_moved_problems() == []


def test_master_moved_silent_on_unparseable_output(monkeypatch):
    monkeypatch.setattr(_mod, "_run", lambda *a, **k: _result("not a number\n"))
    assert _mod.master_moved_problems() == []


# --- main orchestration ------------------------------------------------------


def _run_main(
    monkeypatch,
    stdin,
    *,
    dock=None,
    ok=True,
    targets=None,
    master_moved=None,
    env=None,
    sessions=None,
):
    monkeypatch.setattr(_mod.sys, "stdin", io.StringIO(stdin))
    monkeypatch.setattr(_mod, "docker_problems", lambda: (dock or [], ok))
    monkeypatch.setattr(_mod, "target_problems", lambda: targets or [])
    # stubbed by default: the real one reads this checkout's actual relationship to
    # origin/master, which would make every main() assertion depend on the branch state
    # of whatever worktree happens to be running the suite
    monkeypatch.setattr(_mod, "master_moved_problems", lambda: master_moved or [])
    # stubbed by default: the real one reads this machine's live worktrees, which would
    # make every main() assertion depend on what else happens to be open right now
    monkeypatch.setattr(_mod, "other_live_sessions", lambda cwd: sessions or [])
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


def test_main_lists_other_sessions_even_when_green(monkeypatch, capsys):
    # another session's open work is information this session needs whether or not the
    # homelab itself is healthy, so it is not gated on the health banner
    _run_main(
        monkeypatch, '{"source":"startup"}', sessions=["  • other-branch — roles/k8s/x"]
    )
    assert _mod.main() == 0
    out = capsys.readouterr().out
    assert "Other Claude sessions" in out and "other-branch" in out


def test_main_stays_silent_when_no_other_session_is_live(monkeypatch, capsys):
    _run_main(monkeypatch, '{"source":"startup"}', sessions=[])
    assert _mod.main() == 0
    assert capsys.readouterr().out == ""


def test_main_survives_a_broken_session_scan(monkeypatch, capsys):
    # the scan shells out to git in other checkouts; it must never block session start
    def boom(cwd):
        raise OSError("git exploded")

    _run_main(monkeypatch, '{"source":"startup"}')
    monkeypatch.setattr(_mod, "other_live_sessions", boom)
    assert _mod.main() == 0
    assert capsys.readouterr().out == ""


def test_main_prints_banner_on_problem(monkeypatch, capsys):
    _run_main(
        monkeypatch, '{"source":"startup"}', dock=["  ✗ jellyfin — unhealthy (x)"]
    )
    assert _mod.main() == 0
    assert "jellyfin" in capsys.readouterr().out


def test_main_runs_targets_even_when_docker_down(monkeypatch, capsys):
    # docker_ok=False must no longer short-circuit the Prometheus probe: target_problems()
    # doesn't touch docker (it goes through probe.py's cluster route), so daniel-box (no
    # docker binary at all) used to get a false all-clear on scrape targets — the whole
    # Prometheus check never ran. See the module docstring.
    called = {"targets": False}

    def tp():
        called["targets"] = True
        return ["  ✗ target loki [loki:3100] down"]

    monkeypatch.setattr(_mod.sys, "stdin", io.StringIO('{"source":"startup"}'))
    monkeypatch.setattr(
        _mod, "docker_problems", lambda: (["  ✗ docker unreachable"], False)
    )
    monkeypatch.setattr(_mod, "target_problems", tp)
    assert _mod.main() == 0
    assert called["targets"] is True
    out = capsys.readouterr().out
    assert "docker unreachable" in out
    assert "loki" in out


def test_main_prints_master_moved_line(monkeypatch, capsys):
    _run_main(
        monkeypatch,
        '{"source":"startup"}',
        master_moved=[
            "  ⚠ this branch is 3 commits behind origin/master (as of the last fetch)"
        ],
    )
    assert _mod.main() == 0
    assert "3 commits behind origin/master" in capsys.readouterr().out
