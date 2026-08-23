"""The CHECKS registry itself: what is declared, what is selected, and the loop heartbeat.

`CHECKS` has to agree with the deployed manifests — a check needing an env var the manifest does
not set fails at runtime, and a monitor with no check never beats. CHECKS_ONLY/CHECKS_SKIP are
how the twin deployments split that registry between them.
"""

import os
import re
from pathlib import Path


import check


# ── loop heartbeat (container healthcheck reads this file's mtime) ─────────────


def test_touch_heartbeat_writes_and_refreshes(tmp_path, monkeypatch):
    hb = tmp_path / "heartbeat"
    monkeypatch.setattr(check, "HEARTBEAT_FILE", str(hb))
    check.touch_heartbeat()
    assert hb.exists()
    first = hb.stat().st_mtime
    os.utime(hb, (first - 100, first - 100))  # backdate, then refresh
    check.touch_heartbeat()
    assert hb.stat().st_mtime > first - 100


def test_touch_heartbeat_never_raises(monkeypatch):
    # Best-effort like push(): a heartbeat failure must not kill the loop.
    monkeypatch.setattr(check, "HEARTBEAT_FILE", "/nonexistent-dir/heartbeat")
    check.touch_heartbeat()


# --- CHECKS <-> compose (env + monitors) consistency — CI/CD L2 --------------


def _read_sibling(relpath):
    return (Path(__file__).resolve().parent / relpath).read_text()


def test_checks_and_env_secret_push_tokens_agree():
    # Every KUMA_PUSH_* check.py reads must have an env entry in the env-secret and
    # vice-versa. A check added to CHECKS without its env silently never pushes (empty
    # token) with no Kuma no-heartbeat to self-correct. Single-deployment since the
    # Docker uninstall (2026-08-14) — the remnant compose this used to partition
    # against is archived.

    in_code = set(
        re.findall(r'_env\("(KUMA_PUSH_[A-Z0-9_]+)"', _read_sibling("check.py"))
    )
    in_twin = set(
        re.findall(
            r"^\s*(KUMA_PUSH_[A-Z0-9_]+):",
            _read_sibling("../templates/env-secret.yaml.j2"),
            re.MULTILINE,
        )
    )
    assert in_code == in_twin, "only in check.py=%s ; only in env-secret=%s" % (
        sorted(in_code - in_twin),
        sorted(in_twin - in_code),
    )


def test_every_push_token_env_is_wired_to_a_monitor():
    # Each KUMA_PUSH_* env value var must also appear as a push_token in the
    # kuma-static-monitors Secret, i.e. a push monitor actually exists to receive what
    # the check pushes. (Pre-uninstall this read AutoKuma labels on the remnant compose;
    # the static Secret has been the declaration home for the cluster bridge all along.)

    env_text = _read_sibling("../templates/env-secret.yaml.j2")
    env_vars = set(
        re.findall(r"KUMA_PUSH_[A-Z0-9_]+: \"\{\{ ([a-z0-9_]+) \}\}\"", env_text)
    )
    monitors_text = _read_sibling("../../uptime-kuma/templates/static-monitors.yaml.j2")
    label_vars = set(
        re.findall(r'"push_token": "\{\{ ([a-z0-9_]+) \}\}"', monitors_text)
    )
    assert env_vars, "no KUMA_PUSH_* env vars parsed — regex drift?"
    assert env_vars <= label_vars, (
        "env push tokens with no monitor declared: %s" % sorted(env_vars - label_vars)
    )


# --- CHECKS_ONLY / CHECKS_SKIP (the Phase F twin/remnant split) ---------------------------

# The remnant's real config: only the host-state-file checks, every gate off. Three
# since the 2026-08-14 host flips (pi_peers + renovate_alive became direct pushers).
# A representative CHECKS_ONLY subset. No deployment carries a filter since the Docker
# uninstall (2026-08-14) retired the remnant — these tests keep the MECHANISM honest for
# whenever a split is next expressed.
SUBSET_ONLY = frozenset({"gitops_alive", "gitops_status"})


def test_check_enabled_only_and_skip_semantics():
    assert check.check_enabled("disk", frozenset(), frozenset())
    assert check.check_enabled("gitops_alive", SUBSET_ONLY, frozenset())
    assert not check.check_enabled("disk", SUBSET_ONLY, frozenset())
    assert not check.check_enabled("disk", frozenset(), frozenset({"disk"}))
    # skip wins even against an explicit only-listing
    assert not check.check_enabled("disk", frozenset({"disk"}), frozenset({"disk"}))


def test_name_set_parses_csv_with_spaces():
    assert check._name_set(" a, b ,c,,") == frozenset({"a", "b", "c"})
    assert check._name_set("") == frozenset()


def test_validate_rejects_unknown_names():
    problems = check.validate_check_filter(
        frozenset({"no_such_check"}), frozenset({"also_bogus"}), check.CHECKS
    )
    assert any("no_such_check" in p for p in problems)
    assert any("also_bogus" in p for p in problems)


def test_validate_rejects_enabled_dependent_with_disabled_gate():
    # Skipping the prometheus gate while its dependents still run would reintroduce the
    # one-outage-N-page storm the gate exists to prevent.
    problems = check.validate_check_filter(
        frozenset(), frozenset({"prometheus"}), check.CHECKS
    )
    assert len(problems) == 1
    assert "gate prometheus is disabled" in problems[0]


def test_validate_accepts_only_and_skip_shapes():
    # Both filter directions of a gate-free subset must validate clean.
    assert check.validate_check_filter(SUBSET_ONLY, frozenset(), check.CHECKS) == []
    assert check.validate_check_filter(frozenset(), SUBSET_ONLY, check.CHECKS) == []


def test_subset_names_are_real_checks():
    # Guard (mirrors the PROM_DEPENDENT guard): the subset must track CHECKS renames.
    names = {name for name, _, _ in check.CHECKS}
    assert SUBSET_ONLY <= names


def test_run_once_with_only_filter_touches_no_gate(monkeypatch):
    # With a CHECKS_ONLY filter active, run_once must evaluate exactly that set — no
    # gate probe, no metric check, no push for anything else.
    monkeypatch.setattr(check, "CHECKS_ONLY", SUBSET_ONLY)
    monkeypatch.setattr(check, "CHECKS_SKIP", frozenset())
    evaluated = []
    monkeypatch.setattr(
        check, "_evaluate", lambda name, fn: (evaluated.append(name), (True, "ok"))[1]
    )
    pushed = []
    monkeypatch.setattr(check, "push", lambda token, ok, msg: pushed.append(msg))
    check.run_once()
    assert set(evaluated) == SUBSET_ONLY
    assert len(pushed) == len(SUBSET_ONLY)
