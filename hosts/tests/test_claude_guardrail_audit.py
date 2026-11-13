"""claude-guardrail-audit.sh, run for real against a stateful `auditctl` stub.

The stub keeps its loaded rules in a file and prints them back the way auditctl does: a file
watch as `-w PATH -p wa -k KEY`, a directory watch as `-a always,exit -F dir=PATH/ ... -F
key=KEY`. Each declared path is a file or directory under `tmp_path`, laid over the defaults.

Run: uv run pytest hosts/tests/test_claude_guardrail_audit.py
"""

import os
import re
import subprocess

from _helpers import load_defaults
from _ruleset_drift import textfile_series
from _shell_render import render_shell_script
from lib.proc_testing import fake_bin, path_with, write_exec
from lib.repo_paths import REPO

TEMPLATE = ("components", "claude-code", "claude-guardrail-audit.sh.j2")
CHECK_LIB = "/usr/lib/homelab/homelab-check-lib.sh"
REAL_CHECK_LIB = REPO / "hosts/components/check-lib/files/homelab-check-lib.sh"
DEFAULTS = load_defaults(REPO / "hosts" / "components" / "claude-code")
RENOVATE_DEFAULTS = load_defaults(REPO / "hosts" / "components" / "renovate-agent")
HARDENING_WATCH = "-w /etc/passwd -p wa -k identity"

AUDITCTL = r"""#!/usr/bin/env bash
rules="$AUDIT_RULES"
touch "$rules"
case "$1" in
  -s) printf 'enabled %s\nfailure 1\npid %s\n' "${AUDIT_ENABLED:-1}" "${AUDIT_PID:-812}" ;;
  -l) if [ -s "$rules" ]; then cat "$rules"; else echo "No rules"; fi ;;
  -w)
    [ -n "${AUDIT_REFUSE:-}" ] && [ "$2" = "$AUDIT_REFUSE" ] && exit 1
    if [ -d "$2" ]; then
      echo "-a always,exit -F dir=$2/ -F perm=wa -F key=$6" >> "$rules"
    else
      echo "-w $2 -p wa -k $6" >> "$rules"
    fi ;;
  -W)
    grep -v -e "^-w $2 -p wa -k $6\$" -e "dir=$2/ .*key=$6\$" "$rules" > "$rules.new"
    mv "$rules.new" "$rules" ;;
  *) exit 2 ;;
esac
"""


def _tree(tmp_path):
    """A settings file, a hooks directory, a memory directory, and one declared path not made."""
    home = tmp_path / "home"
    (home / "hooks").mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text("{}\n")
    (home / "memory").mkdir(exist_ok=True)
    return {
        "claude_code_guardrail_audit_paths": [
            str(home / "settings.json"),
            str(home / "hooks"),
            str(home / "CLAUDE.md"),
        ],
        "claude_code_guardrail_audit_memory_dirs": [str(home / "memory")],
    }


def run(tmp_path, overrides=None, rules=(), **env):
    """Render with `overrides` over the tree, run it, and return (rc, status, message, rules).

    `rules` seeds the stub's loaded rules; `env` sets the stub's AUDIT_* knobs.
    """
    body = render_shell_script(
        *TEMPLATE, overrides={**_tree(tmp_path), **(overrides or {})}
    )
    assert CHECK_LIB in body
    bindir = tmp_path / "bin"
    body = body.replace(CHECK_LIB, str(REAL_CHECK_LIB))
    script = write_exec(tmp_path / "claude-guardrail-audit", body)
    journal = tmp_path / "journal"
    fake_bin(
        bindir,
        auditctl=AUDITCTL,
        logger='#!/usr/bin/env bash\nshift 2\nprintf "%s\\n" "$*" >> "$LOGGER_OUT"\n',
    )
    rules_file = tmp_path / "rules"
    rules_file.write_text("".join(f"{r}\n" for r in rules))
    (tmp_path / "textfile").mkdir(exist_ok=True)
    proc = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "PATH": path_with(bindir),
            "LOGGER_OUT": str(journal),
            "TEXTFILE_DIR": str(tmp_path / "textfile"),
            "AUDIT_RULES": str(rules_file),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    [line] = [
        m
        for m in map(
            re.compile(r"status=(up|down) (.*)").fullmatch,
            journal.read_text().splitlines(),
        )
        if m
    ][-1:]
    return proc.returncode, line[1], line[2], rules_file.read_text().splitlines()


def test_an_armed_run_watches_every_present_path_is_clean(tmp_path):
    rc, status, msg, rules = run(tmp_path, rules=[HARDENING_WATCH])
    home = tmp_path / "home"
    assert (rc, status) == (0, "up"), msg
    assert "3 present paths watched" in msg and "1 declared paths not present" in msg
    assert f"-w {home}/settings.json -p wa -k claude-guardrail" in rules
    assert (
        f"-a always,exit -F dir={home}/hooks/ -F perm=wa -F key=claude-guardrail"
        in rules
    )
    assert (
        f"-a always,exit -F dir={home}/memory/ -F perm=wa -F key=claude-memory" in rules
    )
    assert HARDENING_WATCH in rules
    assert (
        textfile_series(tmp_path, "claude-guardrail-audit")["homelab_check_success"]
        == "1"
    )


def test_a_second_run_loads_nothing_twice(tmp_path):
    _, _, _, first = run(tmp_path)
    rc, status, msg, second = run(tmp_path, rules=first)
    assert (rc, status) == (0, "up")
    assert second == first
    assert "added []" in msg


def test_a_stopped_auditd_is_flagged(tmp_path):
    rc, status, msg, _ = run(tmp_path, AUDIT_PID="0")
    assert (rc, status) == (1, "down")
    assert "auditd is not running (pid 0)" in msg
    assert (
        textfile_series(tmp_path, "claude-guardrail-audit")["homelab_check_success"]
        == "0"
    )


def test_kernel_auditing_off_is_flagged(tmp_path):
    rc, status, msg, _ = run(tmp_path, AUDIT_ENABLED="0")
    assert (rc, status) == (1, "down")
    assert "kernel auditing is off (enabled=0)" in msg


def test_a_watch_that_will_not_load_is_flagged_by_path(tmp_path):
    settings = str(tmp_path / "home" / "settings.json")
    rc, status, msg, _ = run(tmp_path, AUDIT_REFUSE=settings)
    assert (rc, status) == (1, "down")
    assert f"1 present paths have no watch [{settings}]" in msg


def test_a_watch_the_list_dropped_is_removed_and_other_keys_are_kept(tmp_path):
    stale = "-w /etc/claude-old -p wa -k claude-guardrail"
    rc, status, msg, rules = run(tmp_path, rules=[HARDENING_WATCH, stale])
    assert (rc, status) == (0, "up")
    assert "removed [/etc/claude-old]" in msg
    assert stale not in rules and HARDENING_WATCH in rules


def test_disarming_removes_every_watch_under_both_keys(tmp_path):
    _, _, _, armed = run(tmp_path, rules=[HARDENING_WATCH])
    rc, status, msg, rules = run(
        tmp_path, {"claude_code_guardrail_audit_enabled": False}, rules=armed
    )
    assert (rc, status) == (0, "up")
    assert "removed 3 watches" in msg
    assert rules == [HARDENING_WATCH]


def test_the_defaults_watch_both_memory_stores_and_the_renovate_agent_home():
    assert DEFAULTS["claude_code_guardrail_audit_memory_dirs"] == [
        "{{ claude_code_operator_memory_dir }}",
        "{{ claude_code_agent_memory_dir }}",
    ]
    renovate = [
        p
        for p in DEFAULTS["claude_code_guardrail_audit_paths"]
        if "renovate-agent" in p
    ]
    assert renovate == [
        f"{RENOVATE_DEFAULTS['renovate_agent_home']}/.claude/settings.json"
    ]
