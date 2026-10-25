"""Tests for `probe.py gitops-state --why` (`probe_lib/gitops_why.py`).

The journal lines are the ones `gitops-deploy.service` logged on 2026-10-17 around the
home-assistant rollback that wrote `hold_sha` at 06:33:33, in `host_reader.READER`'s shape.
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.probe_lib import core, gitops_view, gitops_why
from lib.exit_codes import PROBE_UNREADABLE

HELD_AT = 1792218813  # 2026-10-17 06:33:33 UTC
NOW = HELD_AT + 3600.0
SHA = "2d25ced3" + "0" * 32

TICK = [
    [HELD_AT - 634.0, 5, "held", "flock", "k8s-deploy: deploying home-assistant (timeout 1440s)"],
    [HELD_AT - 600.0, 5, "held", "sudo", "  ubuntu : TTY=pts/3 ; COMMAND=/bin/sh -c 'echo BECOME-SUCCESS-x'"],
    [HELD_AT - 1.0, 3, "held", "flock", "[DEPRECATION WARNING]: INJECT_FACTS_AS_VARS default to `True` is deprecated"],
    [HELD_AT + 0.0, 3, "held", "flock", "k8s-deploy: failed for ['home-assistant']: uv run --frozen ansible-playbook ansible/deploy.yml --tags home-assistant -> 2"],
    [HELD_AT + 0.0, 3, "held", "flock", "TASK [k8s/manifests : Wait for the queued rollouts to finish] ******************"],
    [HELD_AT + 0.0, 3, "held", "flock", 'fatal: [daniel-box]: FAILED! => {"changed": false, "cmd": "k3s kubectl -n homelab rollout status"}'],
    [HELD_AT + 1.0, 5, "held", "flock", "k8s-deploy: deploying home-assistant (timeout 1620s)"],
]  # fmt: skip
EARLIER = [[HELD_AT - 1200.0, 3, "before", "flock", "broad: apply failed (x): y"]]
LATER = [[HELD_AT + 600.0, 3, "after", "flock", "k8s-deploy: failed for ['radarr']: z"]]


def _entries(rows):
    doc = {"journal": {"deployer": {"rc": 0, "stderr": "", "entries": rows}}}
    return gitops_why.journal_entries(doc, "deployer")


def _held_state(tmp_path):
    (tmp_path / gitops_view.MARKERS["hold"]).write_text(SHA)
    os.utime(tmp_path / gitops_view.MARKERS["hold"], (HELD_AT, HELD_AT))
    return tmp_path


def _read(rows, seen=None):
    def read(plan, host):
        if seen is not None:
            seen.append(plan)
        return {
            "host": host,
            "user": "ubuntu",
            "journal": {"deployer": {"rc": 0, "stderr": "", "entries": rows}},
        }

    return read


def test_the_tick_that_held_is_the_run_nearest_the_hold_not_its_neighbours():
    tick = gitops_why.held_tick(_entries(EARLIER + TICK + LATER), HELD_AT)
    assert {e.invocation for e in tick} == {"held"}


def test_only_the_deployer_s_failures_and_ansible_s_fatal_lines_are_kept():
    kept = [e.message for e in _entries(TICK) if gitops_why.is_failure_line(e)]
    assert kept == [TICK[3][4], TICK[4][4], TICK[5][4]]


def test_why_prints_the_failure_lines_of_the_tick_that_held(tmp_path, capsys):
    seen = []
    ns = SimpleNamespace(json=False, why=True)
    rc = gitops_view.run_gitops_state(
        ns, _held_state(tmp_path), NOW, read=_read(EARLIER + TICK + LATER, seen)
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "why:          hold_sha written 2026-10-17 06:33:33 UTC" in out
    assert "k8s-deploy: failed for ['home-assistant']" in out
    assert "fatal: [daniel-box]: FAILED!" in out
    assert "radarr" not in out and "DEPRECATION" not in out
    unit, since, until = seen[0]["journal"]["deployer"]
    assert unit == "gitops-deploy.service"
    assert since == f"@{HELD_AT - gitops_why.tick_ceiling_s()}"
    assert until == f"@{HELD_AT + gitops_why.AFTER_S}"


def test_why_under_json_adds_a_why_key(tmp_path, capsys):
    ns = SimpleNamespace(json=True, why=True)
    gitops_view.run_gitops_state(ns, _held_state(tmp_path), NOW, read=_read(TICK))
    why = json.loads(capsys.readouterr().out)["why"]
    assert why["held_at"] == HELD_AT and why["invocation"] == "held"
    assert len(why["lines"]) == 3


def test_no_hold_means_nothing_to_explain_and_no_read(tmp_path, capsys):
    def no_read(*_a):
        raise AssertionError("no hold, so no journal read")

    ns = SimpleNamespace(json=False, why=True)
    assert gitops_view.run_gitops_state(ns, tmp_path, NOW, read=no_read) == 0
    assert "no hold_sha, so no tick to explain" in capsys.readouterr().out


def test_a_journal_that_cannot_be_read_is_unreadable(tmp_path, capsys):
    def refused(plan, host):
        raise core.Unreadable("could not read daniel-box: exit 255")

    ns = SimpleNamespace(json=False, why=True)
    rc = gitops_view.run_gitops_state(ns, _held_state(tmp_path), NOW, read=refused)
    assert rc == PROBE_UNREADABLE
    assert (
        "why:          unreadable: could not read daniel-box" in capsys.readouterr().out
    )


def test_a_rotated_journal_says_no_run_covers_the_hold(tmp_path, capsys):
    ns = SimpleNamespace(json=False, why=True)
    assert (
        gitops_view.run_gitops_state(ns, _held_state(tmp_path), NOW, read=_read([]))
        == 0
    )
    assert (
        "no gitops-deploy.service run in the journal covers" in capsys.readouterr().out
    )


def test_an_unreadable_hold_is_not_reported_as_no_hold(tmp_path, capsys):
    _held_state(tmp_path)
    (tmp_path / gitops_view.MARKERS["hold"]).chmod(0o000)
    try:
        if os.access(tmp_path / gitops_view.MARKERS["hold"], os.R_OK):
            pytest.skip("running as root, which reads a 0000 file")
        rc = gitops_view.run_gitops_state(
            SimpleNamespace(json=False, why=True), tmp_path, NOW, read=_read(TICK)
        )
    finally:
        (tmp_path / gitops_view.MARKERS["hold"]).chmod(0o600)
    assert rc == PROBE_UNREADABLE
    out = capsys.readouterr().out
    assert "no hold_sha" not in out and "hold_sha is unreadable" in out
