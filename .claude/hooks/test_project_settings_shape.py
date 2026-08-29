"""Guards the shape of this repo's checked-in `.claude/settings.json`.

A `permissions.allow` entry here is a standing grant carried by the repository, so it
reaches every operator who opens the repo rather than only the person who wrote it. Two
properties are worth holding still, and neither is visible from reading the file:

Every allowed `kubectl` verb must be read-only. Until 2026-08-29 this file allowed
sixteen write verbs -- apply, create, patch, set, exec, cp, port-forward, scale, the four
rollout forms, label, annotate, cordon, uncordon. They were inert on the day they were
written, because the cluster credential is a read-only ServiceAccount and RBAC refuses
every one of them, and inert again in a normal session, because `autoMode.classifyAllShell`
suspends `Bash()` allow rules and hands the whole line to the classifier. Neither
protection is a property of this file. Manual mode runs no classifier, and a widened
credential is exactly the change that would make the grant live -- so the grant would
start mattering at the moment it was most dangerous, with nothing here having changed.
Ansible is the write path to this cluster; see docs/claude-shell-permissions.md.

A `Bash()` rule here cannot narrow a verb by flag. Measured 2026-08-08 against the OTEL
`tool_decision` stream: `Bash(kubectl apply --prune*)` fired neither as an `ask` nor as a
`deny`. So a rule that reads like a flag-level guard is a guarantee that is not there.
(The chezmoi-managed user-level rules are the exception, and only because
`allow-compound-bash.sh` glob-matches them itself -- that hook is not in play for this
file's plain verb prefixes.)

Each test below is a pair: one input the rule must reject and one it must accept. A shape
guard that fires on everything and one that fires on nothing look identical from the
passing side, and this repo has paid for that twice.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

SETTINGS = Path(__file__).resolve().parents[1] / "settings.json"

# Verbs that only read. Anything else in an allow rule fails the test below rather than
# being judged case by case -- a new kubectl verb should have to be argued for once, in
# this list, instead of arriving unremarked inside a settings edit.
READ_ONLY_VERBS = frozenset(
    {
        "get",
        "logs",
        "describe",
        "top",
        "explain",
        "events",
        "api-resources",
        "api-versions",
        "version",
        "diff",
        "wait",
        "auth",
        "cluster-info",
    }
)

BASH_RULE = re.compile(r"^Bash\((?P<body>.*)\)$")


def allow_rules() -> list[str]:
    data = json.loads(SETTINGS.read_text())
    return list(data.get("permissions", {}).get("allow", []))


def kubectl_verb(rule: str) -> str | None:
    """The verb a `Bash(kubectl <verb> ...)` rule grants, or None if it is not one."""
    match = BASH_RULE.match(rule)
    if match is None:
        return None
    words = match.group("body").split()
    if len(words) < 2 or words[0] != "kubectl":
        return None
    return words[1]


def test_settings_parses():
    """A settings file that fails to parse is ignored wholesale and nothing warns."""
    assert isinstance(json.loads(SETTINGS.read_text()), dict)


def test_every_allowed_kubectl_verb_is_read_only():
    granted = {v for v in (kubectl_verb(r) for r in allow_rules()) if v is not None}
    writes = sorted(granted - READ_ONLY_VERBS)
    assert not writes, (
        f"{SETTINGS} allows kubectl write verb(s) {writes}. Ansible is the write path to "
        "this cluster; a standing grant here reaches every operator who opens the repo "
        "and becomes live the moment the read-only ServiceAccount is widened. Add the "
        "verb to READ_ONLY_VERBS only if it genuinely reads."
    )


def test_the_read_only_check_rejects_a_write_verb():
    """The RED half. Without it, a check that stopped matching would look clean."""
    assert kubectl_verb("Bash(kubectl apply *)") == "apply"
    assert "apply" not in READ_ONLY_VERBS


def test_the_read_only_check_accepts_a_read_verb():
    assert kubectl_verb("Bash(kubectl get *)") == "get"
    assert "get" in READ_ONLY_VERBS


@pytest.mark.parametrize(
    "rule",
    [
        "Bash(ansible-lint *)",
        "Bash(git check-ignore *)",
        "Bash(uv run python scripts/diagnostics/probe.py:*)",
    ],
)
def test_non_kubectl_rules_are_not_misread_as_kubectl(rule):
    assert kubectl_verb(rule) is None


def test_no_rule_here_pretends_to_narrow_a_verb_by_flag():
    """An interior `-flag` in a rule reads as a guard the native matcher cannot enforce."""
    offenders = [r for r in allow_rules() if re.search(r"\s-{1,2}[A-Za-z]", r)]
    assert not offenders, (
        f"{offenders} look like flag-level guards. A Bash() rule in THIS file matches on "
        "the command and verb only -- measured 2026-08-08, `Bash(kubectl apply --prune*)` "
        "fired as neither ask nor deny. Allow the whole verb or none of it."
    )


def test_the_flag_check_would_catch_one():
    """The RED half of the rule above."""
    assert re.search(r"\s-{1,2}[A-Za-z]", "Bash(kubectl apply --prune*)")
    assert not re.search(r"\s-{1,2}[A-Za-z]", "Bash(kubectl get *)")


def hook_entries() -> list[tuple[str, dict]]:
    """Every `{type, command, ...}` entry, paired with the event it fires on."""
    data = json.loads(SETTINGS.read_text())
    return [
        (event, entry)
        for event, groups in data.get("hooks", {}).items()
        for group in groups
        for entry in group.get("hooks", [])
    ]


def test_every_hook_entry_declares_a_timeout():
    """An omitted timeout is 60s, which is a stall an operator reads as a hung session.

    These hooks run on the interactive path -- a PreToolUse hook sits between the model
    and every Bash call it makes -- so the cost of one hanging is paid on every keystroke
    after it. Four entries here omitted it until 2026-08-29 and nothing reported them,
    because the default is silent and generous rather than absent.
    """
    missing = sorted(
        f"{event}:{entry.get('command', '?')}"
        for event, entry in hook_entries()
        if "timeout" not in entry
    )
    assert not missing, (
        f"hook entries without a timeout: {missing}. Declare one -- the 60s default is "
        "long enough to read as a hung session on the interactive path."
    )


def test_the_timeout_check_reads_the_entries_it_claims_to():
    """The RED half: prove the walk actually reaches entries, not an empty list."""
    entries = hook_entries()
    assert len(entries) >= 5, "the hook walk found almost nothing — the shape changed"
    assert any(e.get("command", "").endswith("session-health.sh") for _, e in entries)
