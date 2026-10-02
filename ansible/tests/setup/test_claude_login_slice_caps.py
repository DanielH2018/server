#!/usr/bin/env python3
"""Login-session caps must reach a session started outside claude-rc.service's cgroup.

claude-rc.service's MemoryHigh, MemorySwapMax and PYTEST_XDIST_AUTO_NUM_WORKERS bound only
that unit's own cgroup. A session started with `claude agents` (or a bare `claude`) from an
interactive SSH shell lands in user.slice/user-<uid>.slice/session-<n>.scope instead, which
none of those three touch.

This module tests the two artifacts that close that gap:

1. `login-slice-caps.conf.j2` — a systemd drop-in on user-<uid>.slice, read by every login
   session's cgroup regardless of how the session started.
2. `pytest-fanout-cap.conf.j2` — a `~/.config/environment.d/` file, read by pam_systemd into
   every login session's PAM environment at session start.

Both must render from the SAME variables the unit already uses
(claude_code_rc_memory_high, claude_code_rc_memory_swap_max, claude_code_rc_pytest_workers)
rather than a second hardcoded number — a value that renders once and is then copy-pasted
would drift the moment either place is tuned without the other, which is exactly the
"depends on how the session started" failure. Each check below is
tested as a pair: one render it must accept, one override it must follow, per this repo's
"a new check ships with a proof it can go RED" rule.

Run: uv run pytest ansible/tests/setup/test_claude_login_slice_caps.py
"""

import re

import pytest
from _helpers import ANSIBLE, load_yaml
from _setup_render import render_setup_text

ROLE = "claude_code"
ROLE_DIR = ANSIBLE / "roles" / "setup" / ROLE
TEMPLATES = ROLE_DIR / "templates"
DEFAULTS = ROLE_DIR / "defaults" / "main.yml"
TASKS = ROLE_DIR / "tasks" / "main.yml"

SLICE_UNIT = "login-slice-caps.conf.j2"
PYTEST_UNIT = "pytest-fanout-cap.conf.j2"


def render(template: str, **overrides: object) -> str:
    """One of this role's templates rendered at inventory values, `overrides` on top.

    Through `_setup_render` rather than a synthetic context this module keeps of its own: a
    context written beside the assertions drifts from defaults/main.yml silently, and the
    assertion then checks a number the host never renders (#3202).
    """
    return render_setup_text(ROLE, template, overrides)


@pytest.fixture(scope="module")
def defaults() -> dict[str, object]:
    """The role defaults the renders above start from, for the expected VALUES.

    Read from defaults rather than written as a literal here: the literal is the drift.
    """
    parsed = load_yaml(DEFAULTS)
    missing = {
        "claude_code_rc_memory_high",
        "claude_code_rc_memory_swap_max",
        "claude_code_rc_pytest_workers",
    } - set(parsed)
    assert not missing, (
        f"defaults/main.yml no longer defines {sorted(missing)} — the login-session caps "
        "have no owner, and every comparison below would pass over an empty set"
    )
    return parsed


def test_both_login_cap_templates_still_exist() -> None:
    """`render` fails on a missing template with its path; this says what losing it MEANS."""
    assert (TEMPLATES / SLICE_UNIT).is_file(), (
        f"{SLICE_UNIT} is missing — the login-slice drop-in is gone"
    )
    assert (TEMPLATES / PYTEST_UNIT).is_file(), (
        f"{PYTEST_UNIT} is missing — the login-session pytest cap is gone"
    )


def test_slice_memory_high_follows_the_same_variable_as_the_unit(
    defaults: dict[str, object],
) -> None:
    """A hardcoded cap here would pass a presence check while ignoring claude_code_rc_memory_high."""
    high = defaults["claude_code_rc_memory_high"]
    rendered = render(SLICE_UNIT)
    assert f"MemoryHigh={high}" in rendered

    raised = render(SLICE_UNIT, claude_code_rc_memory_high="97G")
    assert "MemoryHigh=97G" in raised and f"MemoryHigh={high}" not in raised, (
        "MemoryHigh in login-slice-caps.conf.j2 must render from claude_code_rc_memory_high "
        "— the same variable the RC unit uses — or raising one leaves the other stale"
    )


def test_slice_memory_swap_max_follows_the_same_variable_as_the_unit(
    defaults: dict[str, object],
) -> None:
    swap = defaults["claude_code_rc_memory_swap_max"]
    rendered = render(SLICE_UNIT)
    assert f"MemorySwapMax={swap}" in rendered

    raised = render(SLICE_UNIT, claude_code_rc_memory_swap_max="96G")
    assert "MemorySwapMax=96G" in raised and f"MemorySwapMax={swap}" not in raised, (
        "MemorySwapMax in login-slice-caps.conf.j2 must render from "
        "claude_code_rc_memory_swap_max, or raising the unit's cap leaves the login "
        "session's cap behind"
    )


def test_slice_has_no_memory_max() -> None:
    """MemoryMax on a login-session slice would kill every process in every session at once,
    including the SSH connection running the deploy — the same reasoning the RC unit's
    CLAUDE.md gives for never setting it there."""
    rendered = render(SLICE_UNIT)
    assert not re.search(r"^MemoryMax=", rendered, re.M), (
        "MemoryMax applies to the whole slice: it would OOM-kill every login session for "
        "this user at once, including the one running the deploy"
    )


def test_slice_drop_in_targets_the_configured_uid() -> None:
    """The dest path must key off claude_code_login_uid, not a literal 1000."""
    tasks = TASKS.read_text()
    assert "user-{{ claude_code_login_uid }}.slice.d" in tasks, (
        "the login-slice drop-in's destination must be derived from claude_code_login_uid "
        "— a hardcoded user-1000.slice.d would silently miss any host where that account "
        "has a different uid"
    )


def test_pytest_cap_follows_the_same_variable_as_the_unit(
    defaults: dict[str, object],
) -> None:
    """A hardcoded worker count here is the failure the RC unit's own test guards against."""
    workers = defaults["claude_code_rc_pytest_workers"]
    rendered = render(PYTEST_UNIT)
    assert f"PYTEST_XDIST_AUTO_NUM_WORKERS={workers}" in rendered

    raised = render(PYTEST_UNIT, claude_code_rc_pytest_workers=97)
    assert (
        "PYTEST_XDIST_AUTO_NUM_WORKERS=97" in raised
        and f"PYTEST_XDIST_AUTO_NUM_WORKERS={workers}" not in raised
    ), (
        "PYTEST_XDIST_AUTO_NUM_WORKERS in pytest-fanout-cap.conf.j2 must render from "
        "claude_code_rc_pytest_workers — the same variable the RC unit's Environment= line "
        "reads — or raising the unit's cap leaves a login session's cap at the old value"
    )


def test_login_caps_var_defaults_to_enabled() -> None:
    assert re.search(
        r"^claude_code_login_caps_enabled: *true\b", DEFAULTS.read_text(), re.M
    ), (
        "claude_code_login_caps_enabled must default to true, or the login-session caps "
        "this issue adds ship disabled"
    )


def test_login_caps_can_be_turned_off() -> None:
    """The reverse-states rule: a way to disable the caps needs to exist, not just a way to
    enable them. Both artifacts must have a `when: not claude_code_login_caps_enabled`
    removal task, or setting the var false leaves a stale drop-in / environment.d file that
    nothing ever cleans up."""
    tasks = TASKS.read_text()
    assert "when: not claude_code_login_caps_enabled" in tasks, (
        "no task removes the login-session caps when claude_code_login_caps_enabled is set "
        "false — a one-way door: the caps could be turned on but never off"
    )
    assert tasks.count("state: absent") >= 2, (
        "expected an absent-state removal task for both the slice drop-in and the "
        "environment.d file"
    )


def test_login_uid_default_is_a_plain_integer() -> None:
    """claude_code_login_uid must be a static default so the render tests above stay valid
    against what actually deploys, not a getent-derived fact only visible at play time."""
    assert re.search(r"^claude_code_login_uid: *\d+", DEFAULTS.read_text(), re.M), (
        "claude_code_login_uid is gone from defaults — the login-slice drop-in path has no "
        "owner"
    )
