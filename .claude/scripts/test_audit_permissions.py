#!/usr/bin/env python3
"""Tests for the audit-permissions report/suggester (Python port).

Locks the reporting, segment-aware allowlist-suggestion, and policy-block
contract: split compound commands on real (unquoted) separators, never mine
cmdlets out of a quoted powershell one-liner, scope multi-verb tools to their
subcommand, and cross-reference the live allow/deny/ask tiers so already-covered
or deliberately-prompting rules are filtered out.

Run: uv run pytest .claude/scripts
(Loads the script by path; it in turn loads the log-permission hook by path.)
"""
import importlib.util
import os

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit-permissions.py")
_spec = importlib.util.spec_from_file_location("audit_permissions", _SCRIPT)
a = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(a)


def fixture():
    return {
        "version": 1,
        "updated": "2026-06-17T10:00:00.000Z",
        "entries": {
            "k1": {"tool": "Bash", "sum": "git status", "calls": 50, "asks": 0, "first": "2026-06-01T00:00:00.000Z", "last": "2026-06-17T00:00:00.000Z"},
            "k2": {"tool": "Bash", "sum": "git push origin main", "calls": 6, "asks": 6, "first": "2026-06-02T00:00:00.000Z", "last": "2026-06-16T00:00:00.000Z"},
            "k3": {"tool": "Bash", "sum": "git pull", "calls": 3, "asks": 2, "first": "2026-06-03T00:00:00.000Z", "last": "2026-06-10T00:00:00.000Z"},
            "k4": {"tool": "Write", "sum": "/x/y.md", "calls": 10, "asks": 0, "first": "2026-06-04T00:00:00.000Z", "last": "2026-06-15T00:00:00.000Z"},
        },
    }


def test_summarize_report_totals_calls_and_asks():
    rep = a.summarize_report(fixture(), {})
    assert rep["totalCalls"] == 69
    assert rep["totalAsks"] == 8
    assert rep["byTool"]["Bash"]["calls"] == 59
    assert rep["byTool"]["Write"]["calls"] == 10


def test_summarize_report_prompted_sorted_desc_excludes_never_prompted():
    rep = a.summarize_report(fixture(), {})
    assert len(rep["prompted"]) == 2
    assert rep["prompted"][0]["sum"] == "git push origin main"
    assert rep["prompted"][1]["sum"] == "git pull"


def test_summarize_report_since_filters_by_last_seen():
    rep = a.summarize_report(fixture(), {"since": "2026-06-14T00:00:00.000Z"})
    assert rep["totalAsks"] == 6  # k3 (last 2026-06-10) drops out


def test_summarize_report_per_tool_auto_sums_per_entry_max():
    store = {"version": 1, "updated": None, "entries": {
        "e1": {"tool": "Bash", "sum": "a", "calls": 3, "asks": 5, "first": "2026-06-01T00:00:00.000Z", "last": "2026-06-10T00:00:00.000Z"},
        "e2": {"tool": "Bash", "sum": "b", "calls": 10, "asks": 0, "first": "2026-06-01T00:00:00.000Z", "last": "2026-06-10T00:00:00.000Z"},
    }}
    rep = a.summarize_report(store, {})
    assert rep["byTool"]["Bash"]["auto"] == 10


def test_suggest_rules_groups_prompted_bash_by_prefix():
    rep = a.summarize_report(fixture(), {})
    rules = a.suggest_rules(rep["prompted"])
    assert rules[0]["rule"] == "Bash(git push *)"
    assert any(r["rule"] == "Bash(git pull *)" for r in rules)


def test_suggest_rules_emits_exact_rule_for_single_token_commands():
    rules = a.suggest_rules([{"tool": "Bash", "sum": "htop", "asks": 3}])
    assert rules[0]["rule"] == "Bash(htop)"


def test_do_prune_removes_entries_not_seen_within_n_days():
    store = fixture()
    removed = a.do_prune(store, 5, a.lib.parse_ms("2026-06-17T00:00:00.000Z"))
    assert removed == 1
    assert "k3" not in store["entries"]


def test_parse_args_validates_flags_and_values():
    assert a.parse_args(["--json"])["json"] is True
    assert a.parse_args(["--since", "2026-06-01"])["since"] == "2026-06-01"
    assert a.parse_args(["--prune", "30"])["prune"] == 30
    assert a.parse_args(["--prune", "abc"])["error"]
    assert a.parse_args(["--prune"])["error"]
    assert a.parse_args(["--since"])["error"]
    assert a.parse_args(["--since", "notadate"])["error"]
    assert a.parse_args(["--bogus"])["error"]


def test_parse_args_rejects_prune_zero():
    assert a.parse_args(["--prune", "0"])["error"]
    assert a.parse_args(["--prune", "1"])["prune"] == 1


def test_render_handles_empty_and_populated_reports():
    out0 = a.render(a.summarize_report({"version": 1, "updated": None, "entries": {}}, {}))
    assert "0 calls" in out0
    assert "No prompted commands" in out0
    out = a.render(a.summarize_report(fixture(), {}))
    assert "Most-prompted" in out
    assert "Bash(git push *)" in out


def test_split_segments_breaks_compound_commands():
    assert a.split_segments("echo hi && grep foo bar | head -5") == ["echo hi", "grep foo bar", "head -5"]
    assert a.split_segments("a ; b || c") == ["a", "b", "c"]
    assert a.split_segments("solo") == ["solo"]


def test_split_segments_does_not_split_inside_quotes():
    assert a.split_segments('powershell.exe -Command "Get-Process | Where-Object { $_.x }" 2>&1 | head -5') == \
        ['powershell.exe -Command "Get-Process | Where-Object { $_.x }" 2>&1', 'head -5']
    assert a.split_segments("grep 'a;b' file") == ["grep 'a;b' file"]


def test_split_segments_treats_heredoc_body_as_data():
    cmd = "cat > /tmp/diag.ps1 <<'EOF'\nGet-Process | Where-Object { $_.x } | Select-Object Name\nEOF"
    segs = a.split_segments(cmd)
    assert len(segs) == 1
    assert segs[0].startswith("cat > /tmp/diag.ps1")


def test_suggest_rules_does_not_mine_cmdlets_from_powershell_command_string():
    rules = a.suggest_rules([
        {"tool": "Bash", "sum": 'powershell.exe -NoProfile -Command "Get-Process | Where-Object { $_.x } | ForEach-Object { $_ }"', "asks": 9}
    ])
    assert len(rules) == 0


def test_candidate_prefix_uses_subcommand_for_multi_verb_tools():
    assert a.candidate_prefix("git push origin main")["prefix"] == "git push"
    assert a.candidate_prefix("gh api repos/x")["prefix"] == "gh api"
    assert a.candidate_prefix("head -40 file")["prefix"] == "head"  # flag arg, not a subcommand
    assert a.candidate_prefix("ls -la /c")["prefix"] == "ls"
    assert a.candidate_prefix('node "C:/x.js"')["prefix"] == "node"
    assert a.candidate_prefix("htop")["exact"] is True


def test_candidate_prefix_ignores_leading_env_assignment_prefixes():
    assert a.candidate_prefix('PERMLOG_STORE="$T" node x.js')["prefix"] == "node"
    assert a.candidate_prefix("NODE_ENV=test npm run build")["prefix"] == "npm run"
    # a command-substitution assignment is not a simple env prefix → left alone
    assert a.candidate_prefix("T=$(node -e 1) echo hi")["head"] == "T=$(node"


def test_is_covered_matches_wildcard_and_exact_respects_boundaries():
    assert a.is_covered("git push", ["Bash(git *)"]) is True
    assert a.is_covered("ls", ["Bash(ls *)"]) is True
    assert a.is_covered("gh api", ["Bash(gh pr *)"]) is False
    assert a.is_covered("htop", ["Bash(htop)"]) is True
    assert a.is_covered("grep", ["Read", "Bash(grep *)"]) is True


def test_suggest_rules_splits_compound_and_skips_unsafe_noise_heads():
    rules = a.suggest_rules([
        {"tool": "Bash", "sum": 'cd "C:/x" && echo "=== hi ===" && grep foo bar.txt && head -5 bar.txt', "asks": 3}
    ])
    names = [r["rule"] for r in rules]
    assert "Bash(grep *)" in names
    assert "Bash(head *)" in names
    assert not any(n.startswith("Bash(cd") for n in names)
    assert not any(n.startswith("Bash(echo") for n in names)


def test_suggest_rules_filters_segments_already_covered_by_allow():
    prompted = [{"tool": "Bash", "sum": "grep foo bar && wc -l bar", "asks": 4}]
    rules = a.suggest_rules(prompted, {"allow": ["Bash(grep *)"]})
    names = [r["rule"] for r in rules]
    assert "Bash(grep *)" not in names
    assert "Bash(wc *)" in names


def test_cat_left_to_prompt_by_design():
    prompted = [{"tool": "Bash", "sum": "cat somefile.md", "asks": 9}]
    assert len(a.suggest_rules(prompted)) == 0
    blocked = a.blocked_by_policy(prompted)
    assert any(b["prefix"] == "cat" and b["asks"] == 9 for b in blocked)


def test_suggest_rules_never_suggests_powershell_or_unsafe_shells():
    rules = a.suggest_rules([
        {"tool": "Bash", "sum": 'powershell.exe -NoProfile -Command "Get-Process"', "asks": 31}
    ])
    assert len(rules) == 0


def test_blocked_by_policy_reports_unsafe_segment_heads():
    blocked = a.blocked_by_policy([
        {"tool": "Bash", "sum": 'cd "C:/x" && powershell.exe -Command "x"', "asks": 5},
        {"tool": "Bash", "sum": "rm -f /tmp/x", "asks": 2},
    ])
    by_prefix = {b["prefix"]: b["asks"] for b in blocked}
    assert by_prefix["powershell.exe"] == 5
    assert by_prefix["cd"] == 5
    assert by_prefix["rm"] == 2


def test_collect_permissions_merges_allow_deny_ask():
    merged = a.collect_permissions([
        {"permissions": {"allow": ["Bash(git *)"], "deny": ["Bash(rm -rf *)"], "ask": ["Bash(git reset --hard*)"]}},
        {"permissions": {"allow": ["Bash(node *)"]}},
        {"nothing": True},
    ])
    assert merged["allow"] == ["Bash(git *)", "Bash(node *)"]
    assert merged["deny"] == ["Bash(rm -rf *)"]
    assert merged["ask"] == ["Bash(git reset --hard*)"]


def test_suggest_rules_does_not_propose_a_rule_set_to_prompt_ask_tier():
    prompted = [{"tool": "Bash", "sum": "sketchy --do-it", "asks": 5}]
    assert len(a.suggest_rules(prompted)) == 1
    rules = a.suggest_rules(prompted, {"ask": ["Bash(sketchy *)"]})
    assert len(rules) == 0


def test_dead_allow_rules_flags_hook_covered_but_keeps_wildcards_and_non_bash():
    perms = {"allow": [
        "Bash(sensors)",                            # hook auto-approves -> dead
        "Bash(docker exec *)",                      # wildcard, broader than hook -> keep
        "Bash(sops -d ansible/vars/secrets.yml)",   # not read-only -> keep
        "Skill(security-review)",                   # non-Bash -> keep
        "WebFetch(domain:github.com)",              # non-Bash -> keep
    ]}
    dead = a.dead_allow_rules(perms)
    flagged = {d["rule"] for d in dead}
    if a.aar is not None:  # hook classifier loaded by path; present in-repo
        assert "Bash(sensors)" in flagged
        assert any("hook already covers" in d["reason"]
                   for d in dead if d["rule"] == "Bash(sensors)")
    for keep in ("Bash(docker exec *)", "Bash(sops -d ansible/vars/secrets.yml)",
                 "Skill(security-review)", "WebFetch(domain:github.com)"):
        assert keep not in flagged


def test_dead_allow_rules_flags_rule_subsumed_by_broader_wildcard():
    perms = {"allow": ["Bash(uv run *)", "Bash(uv run pytest)"]}
    dead = a.dead_allow_rules(perms)
    assert {d["rule"] for d in dead} == {"Bash(uv run pytest)"}
    assert "subsumed" in dead[0]["reason"]


def test_dead_allow_rules_flags_exact_duplicate_once_keeping_first():
    # "weird-tool" isn't a known read-only command, so only the duplicate is dead.
    perms = {"allow": ["Bash(weird-tool)", "Bash(weird-tool)"]}
    dead = a.dead_allow_rules(perms)
    dups = [d for d in dead if "duplicate" in d["reason"]]
    assert len(dups) == 1 and dups[0]["rule"] == "Bash(weird-tool)"


def test_compute_dead_local_prune_removes_dead_keeps_needed_and_committed_context():
    committed = ["Bash(ansible-lint *)"]              # fixed context, never edited
    local = [
        "Bash(ansible-lint *)",                       # duplicate of committed -> remove
        "Bash(ansible-lint)",                         # subsumed by ansible-lint * -> remove
        "Bash(uv run *)",                             # wildcard, keep
        "Bash(uv run pytest)",                        # subsumed by uv run * -> remove
        "Bash(sops -d ansible/vars/secrets.yml)",     # genuinely needed -> keep
        "Skill(security-review)",                     # non-Bash -> keep
    ]
    new_local, removed = a.compute_dead_local_prune(committed, local)
    assert new_local == [
        "Bash(uv run *)",
        "Bash(sops -d ansible/vars/secrets.yml)",
        "Skill(security-review)",
    ]
    assert {r["rule"] for r in removed} == {
        "Bash(ansible-lint *)", "Bash(ansible-lint)", "Bash(uv run pytest)"}
    # committed list is never returned for editing
    assert "Bash(ansible-lint *)" in committed


def test_compute_dead_local_prune_noop_when_nothing_dead():
    committed = []
    local = ["Bash(uv run *)", "Bash(docker exec *)", "Skill(deploy)"]
    new_local, removed = a.compute_dead_local_prune(committed, local)
    assert removed == [] and new_local == local
