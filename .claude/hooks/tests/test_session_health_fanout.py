#!/usr/bin/env python3
"""Tests for the SessionStart banner's remote fan-out lines.

Split from test_session_health.py (which sits at 492 of a 500-line ceiling) rather than
grown in place. Covers `remote_fanout_lines()`, which reads
`~/.claude/fanout/<run-id>.json` manifests so the banner can name a fan-out worktree that
`git worktree list` cannot see because it was created on the other host.

Run: uv run pytest .claude/hooks/tests/test_session_health_fanout.py
"""

import importlib.util
import json
import os
import shutil
import subprocess

_HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HOOK = os.path.join(_HOOKS, "session-health.py")
_REPO = os.path.dirname(os.path.dirname(_HOOKS))
_spec = importlib.util.spec_from_file_location("session_health", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_a_missing_hooklib_still_leaves_a_banner_naming_it(tmp_path):
    """RED half, isolated to the hooklib import: `scripts/lib` resolves, `hooklib` does not.

    Same shape as test_session_health_module_scope_import.py's park-module-missing case:
    copy the hook with a real `scripts/lib/` beside it but no `hooklib/`, so the only
    import that can fail is `hooklib.worktree_lines`'s, and the banner must name it.
    """
    home = tmp_path / "a" / "b"
    home.mkdir(parents=True)
    copy = home / "session-health.py"
    shutil.copy(_HOOK, copy)
    lib = tmp_path / "scripts" / "lib"
    shutil.copytree(os.path.join(_REPO, "scripts", "lib"), lib)

    result = subprocess.run(
        ["uv", "run", "--no-project", "python", str(copy)],
        input=json.dumps({"source": "startup"}),
        cwd=str(tmp_path),
        env=dict(os.environ, SESSION_HEALTH_VERBOSE="1"),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert "hooklib.worktree_lines is broken" in result.stdout, result.stdout


def test_remote_fanout_lines_name_each_batch_on_another_host(tmp_path):
    (tmp_path / "20260909T203000Z.json").write_text(
        json.dumps(
            {
                "run_id": "20260909T203000Z",
                "orchestrator_branch": "worktree-orch",
                "batches": [
                    {
                        "batch": "1345",
                        "host": "daniel-server",
                        "worktree": "/home/ubuntu/server/.claude/worktrees/fanout-1345",
                        "branch": "worktree-fanout-1345",
                        "unit": "fanout-1345",
                        "issues": [1345],
                        "launched_at": "2026-09-09T20:30:00+00:00",
                    },
                    {
                        "batch": "1386",
                        "host": "daniel-box",
                        "worktree": "/w",
                        "branch": "worktree-fanout-1386",
                        "unit": "fanout-1386",
                        "issues": [1386],
                        "launched_at": "2026-09-09T20:30:00+00:00",
                    },
                ],
            }
        )
    )
    lines = _mod.remote_fanout_lines(manifest_dir=tmp_path, local_host="daniel-box")
    assert lines == [
        "🛰 fan-out worktrees on other hosts "
        "(uv run python scripts/dev/fanout_place.py status <run-id>):",
        "  • daniel-server worktree-fanout-1345 — #1345 (run 20260909T203000Z)",
    ]


def test_the_default_manifest_dir_is_resolved_at_call_time_not_at_import(
    tmp_path, monkeypatch
):
    """Ruling 28: `Path.home()` at module scope raises past the hook's `except ImportError`.

    A HOME set after import is what proves the default is lazy: bound at import, the
    planted manifest below would be invisible and the banner empty.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    fanout = tmp_path / ".claude" / "fanout"
    fanout.mkdir(parents=True)
    (fanout / "r.json").write_text(
        json.dumps(
            {
                "run_id": "r5",
                "batches": [{"host": "daniel-server", "branch": "b", "issues": [7]}],
            }
        )
    )
    lines = _mod.remote_fanout_lines(local_host="daniel-box")
    assert lines[-1] == "  • daniel-server b — #7 (run r5)"


def test_remote_fanout_lines_are_silent_with_no_manifest(tmp_path):
    assert (
        _mod.remote_fanout_lines(manifest_dir=tmp_path, local_host="daniel-box") == []
    )


def test_remote_fanout_lines_are_silent_when_every_batch_is_local(tmp_path):
    (tmp_path / "r.json").write_text(
        json.dumps(
            {
                "run_id": "r",
                "batches": [{"host": "daniel-box", "branch": "b", "issues": [1]}],
            }
        )
    )
    assert (
        _mod.remote_fanout_lines(manifest_dir=tmp_path, local_host="daniel-box") == []
    )


def test_remote_fanout_lines_skips_a_malformed_manifest_but_keeps_the_rest(tmp_path):
    # Not a dict at all -- data.get("batches", ...) would raise AttributeError.
    (tmp_path / "a-bad-list.json").write_text("[]")
    # A dict, but a batch missing the "branch" key the line-builder indexes -- KeyError.
    (tmp_path / "b-bad-missing-branch.json").write_text(
        json.dumps({"run_id": "bad", "batches": [{"host": "daniel-server"}]})
    )
    (tmp_path / "c-good.json").write_text(
        json.dumps(
            {
                "run_id": "r2",
                "batches": [
                    {"host": "daniel-server", "branch": "b", "issues": [2]},
                ],
            }
        )
    )
    lines = _mod.remote_fanout_lines(manifest_dir=tmp_path, local_host="daniel-box")
    assert lines == [
        "🛰 fan-out worktrees on other hosts "
        "(uv run python scripts/dev/fanout_place.py status <run-id>):",
        "  • daniel-server b — #2 (run r2)",
    ]


def test_remote_fanout_lines_skips_a_malformed_batch_but_keeps_its_sibling(tmp_path):
    # Same manifest, two batches: the first is missing "branch", the second is fine.
    # A bad batch must not take its sibling in the SAME manifest down with it.
    (tmp_path / "r.json").write_text(
        json.dumps(
            {
                "run_id": "r3",
                "batches": [
                    {"host": "daniel-server", "issues": [1]},
                    {"host": "daniel-server", "branch": "b", "issues": [2]},
                ],
            }
        )
    )
    lines = _mod.remote_fanout_lines(manifest_dir=tmp_path, local_host="daniel-box")
    assert lines == [
        "🛰 fan-out worktrees on other hosts "
        "(uv run python scripts/dev/fanout_place.py status <run-id>):",
        "  • daniel-server b — #2 (run r3)",
    ]
