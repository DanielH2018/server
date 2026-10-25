"""`probe.py push-rules`: which ruleset rule a push meets, as refuse/pass pairs per rule type.

The rule evaluation is driven through `evaluate` with hand-built `PushFacts`; the end-to-end
cases stub only `subprocess.run`, keyed on the command, and run the real reads above it.

Run: uv run pytest scripts/diagnostics/tests/test_probe_push_rules.py
"""

import json
import os
import subprocess
import sys

import pytest

# `scripts/diagnostics` is deliberately absent from `pythonpath` in pyproject.toml, so this
# module puts its own parent directory on `sys.path` — the insert every sibling here carries.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import probe
from diagnostics.probe_lib import push_rules
from diagnostics.probe_lib.push_rules import PushFacts, evaluate
from lib.exit_codes import PROBE_FAILED, PROBE_OK, PROBE_UNREADABLE

CI, FENCE = 20912512, 24517167
SIGNED = "a" * 40
UNSIGNED = "b" * 40


def ruleset(name, bypass="never", enforcement="active"):
    body = {"name": name, "enforcement": enforcement}
    if bypass is not None:
        body["current_user_can_bypass"] = bypass
    return body


def facts(exists=True, fast_forward=True, unsigned=(), merges=()):
    return PushFacts("topic", exists, fast_forward, list(unsigned), list(merges))


def rule(rtype, rid=FENCE):
    return {"type": rtype, "ruleset_id": rid}


def refused_types(rules, rulesets, push):
    refusing, _ = evaluate(rules, rulesets, push)
    return [row[2] for row in refusing]


def test_an_unsigned_commit_is_refused_by_required_signatures():
    sets = {CI: ruleset("CI gate")}
    rules = [rule("required_signatures", CI)]
    assert refused_types(rules, sets, facts(unsigned=[UNSIGNED])) == [
        "required_signatures"
    ]
    assert refused_types(rules, sets, facts()) == []


def test_creation_binds_a_new_branch_and_update_an_existing_one():
    sets = {FENCE: ruleset("agent branch fence")}
    rules = [rule("creation"), rule("update"), rule("deletion")]
    assert refused_types(rules, sets, facts(exists=False)) == ["creation"]
    assert refused_types(rules, sets, facts(exists=True)) == ["update"]


def test_a_force_push_is_refused_by_non_fast_forward():
    sets = {FENCE: ruleset("Default")}
    rules = [rule("non_fast_forward")]
    assert refused_types(rules, sets, facts(fast_forward=False)) == ["non_fast_forward"]
    assert refused_types(rules, sets, facts(fast_forward=True)) == []


def test_an_always_bypass_moves_the_rule_to_bypassed():
    rules = [rule("update")]
    refusing, bypassed = evaluate(rules, {FENCE: ruleset("fence", "always")}, facts())
    assert refusing == []
    assert [row[0] for row in bypassed] == ["fence"]
    # `pull_requests_only` bypasses on a merge, never on a push.
    refusing, _ = evaluate(
        rules, {FENCE: ruleset("fence", "pull_requests_only")}, facts()
    )
    assert [row[2] for row in refusing] == ["update"]


def test_an_evaluate_mode_ruleset_refuses_nothing():
    sets = {FENCE: ruleset("fence", enforcement="evaluate")}
    assert refused_types([rule("update")], sets, facts()) == []


def test_an_anonymous_read_reports_the_bypass_unknown():
    sets = {FENCE: ruleset("fence", bypass=None)}
    refusing, bypassed = evaluate([rule("update")], sets, facts())
    report = push_rules.format_report(facts(), refusing, bypassed, "", "url")
    assert "REFUSES" in report and "bypass unknown" in report


def test_unsigned_commits_reads_the_signature_header():
    signed = "tree t\nauthor a\ngpgsig -----BEGIN SSH SIGNATURE-----\n x\n\nmsg\n"
    plain = "tree t\nauthor a\n\nmsg mentioning gpgsig in the body\n"
    batch = (
        f"{SIGNED} commit {len(signed)}\n{signed}\n"
        f"{UNSIGNED} commit {len(plain)}\n{plain}\n"
    )
    assert push_rules.unsigned_commits(batch) == [UNSIGNED]


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:DanielH2018/server.git",
        "https://github.com/DanielH2018/server.git",
    ],
)
def test_repo_slug_reads_both_remote_shapes(url):
    assert push_rules.repo_slug(url) == "DanielH2018/server"


def _transport(gh_fails=False, branch_exists=True, ancestor=True, signed=False):
    """A `subprocess.run` answering every read `run_push_rules` makes for a fenced branch."""
    remote_head = "c" * 40
    rules = [rule("update"), rule("required_signatures", CI)]
    bodies = {
        FENCE: ruleset("agent branch fence"),
        CI: ruleset("CI gate"),
    }
    commit = "tree t\ngpgsig x\n\nmsg\n" if signed else "tree t\n\nmsg\n"

    def run(argv, *args, input=None, **kwargs):
        def ok(out=""):
            return subprocess.CompletedProcess(argv, 0, out, "")

        cmd = " ".join(argv)
        if cmd.startswith("git remote get-url"):
            return ok("git@github.com:DanielH2018/server.git\n")
        if argv[0] == "gh":
            if gh_fails:
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 403: rate limit")
            if argv[2] == "user":
                return ok("DanielClaudeBot\n")
            if "/rules/branches/" in argv[2]:
                return ok(json.dumps(rules))
            return ok(json.dumps(bodies[int(argv[2].rsplit("/", 1)[1])]))
        if cmd.startswith("git ls-remote"):
            return ok(f"{remote_head}\trefs/heads/topic\n" if branch_exists else "")
        if cmd.startswith("git rev-parse"):
            return ok(f"{UNSIGNED}\n")
        if cmd.startswith("git merge-base"):
            return subprocess.CompletedProcess(argv, 0 if ancestor else 1, "", "")
        if cmd.startswith("git rev-list --merges"):
            return ok("")
        if cmd.startswith("git rev-list"):
            return ok(f"{UNSIGNED}\n")
        if cmd.startswith("git cat-file"):
            return ok(f"{UNSIGNED} commit {len(commit)}\n{commit}\n")
        raise AssertionError(f"unexpected command: {cmd}")

    return run


def test_a_refused_push_exits_1_naming_each_ruleset_and_reason(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", _transport())
    rc = probe.main(["push-rules", "topic"])
    out = capsys.readouterr().out
    assert rc == PROBE_FAILED
    assert f"'agent branch fence' ({FENCE}) rule update" in out
    assert (
        f"'CI gate' ({CI}) rule required_signatures: unsigned commits: {UNSIGNED[:9]}"
        in out
    )
    assert "gh login DanielClaudeBot" in out


def test_a_push_no_rule_binds_exits_0(monkeypatch, capsys):
    # A new branch of signed commits trips neither `update` nor `required_signatures`.
    monkeypatch.setattr(subprocess, "run", _transport(branch_exists=False, signed=True))
    assert probe.main(["push-rules", "topic"]) == PROBE_OK
    assert "no active ruleset rule binds this push" in capsys.readouterr().out


def test_a_failed_rules_read_exits_75_with_the_error_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", _transport(gh_fails=True))
    assert probe.main(["push-rules", "topic"]) == PROBE_UNREADABLE
    assert "rate limit" in capsys.readouterr().err
