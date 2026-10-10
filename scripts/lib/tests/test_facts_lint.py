"""Each lint rule as a clean/flagged pair over a one-file repo."""

import json

import pytest

from lib.ansible_inventory import host_names
from lib.git_testing import git, init_repo
from lib.facts.lint import (
    INVENTORY_REL,
    RULES,
    WARN_RULES,
    LintFinding,
    changed_units,
    host_pattern,
    lint_sections,
)
from lib.facts.lock import LOCK_REL, write_lock
from lib.repo_paths import REPO


def _repo(tmp_path, doc):
    init_repo(tmp_path)
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f.txt").write_text("x\n")
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "m.py").write_text("LIMIT = 85\n# DECIDED: keep it\n")
    (tmp_path / "t" / "test_m.py").write_text("def test_a():\n    assert True\n")
    (tmp_path / "CLAUDE.md").write_text(doc)
    git(tmp_path, "add", "-A")
    return tmp_path


def _rules(tmp_path, doc):
    repo = _repo(tmp_path, doc)
    return {(f.unit, f.rule) for f in lint_sections(repo, None)}


def test_clean_section_has_no_findings(tmp_path):
    assert (
        _rules(
            tmp_path, "## A\n`t/m.py:LIMIT` and `d/` and `t/m.py:DECIDED: keep it`.\n"
        )
        == set()
    )


def test_out_of_tree_citation_is_ignored(tmp_path):
    """A slashed token that names no repo root is prose: neither an error nor a warning."""
    assert (
        _rules(
            tmp_path, "## A\nrebase onto `origin/master`, then `defaults/main.yml`\n"
        )
        == set()
    )


def test_file_line_is_flagged(tmp_path):
    assert ("CLAUDE.md#A", "rejected-form") in _rules(
        tmp_path, "## A\nsee `t/m.py:1`\n"
    )


def test_dir_without_slash_is_flagged(tmp_path):
    repo = _repo(tmp_path, "## A\nsee `d/sub`\n")
    (repo / "d" / "sub").mkdir()
    (repo / "d" / "sub" / "g.txt").write_text("g\n")
    git(repo, "add", "d/sub")
    assert ("CLAUDE.md#A", "dir-without-slash") in {
        (f.unit, f.rule) for f in lint_sections(repo, None)
    }


def test_dir_with_slash_is_clean(tmp_path):
    repo = _repo(tmp_path, "## A\nsee `d/sub/`\n")
    (repo / "d" / "sub").mkdir()
    (repo / "d" / "sub" / "g.txt").write_text("g\n")
    git(repo, "add", "d/sub")
    assert not lint_sections(repo, None)


_RENOVATE = {
    "customManagers": [
        {
            "managerFilePatterns": ["/(^|/)v\\.yml$/"],
            "matchStrings": [
                "_image:\\s*(?<depName>[^:\\s@]+):(?<currentValue>[^\\s@]+)"
            ],
        }
    ]
}


def _pinned_repo(tmp_path, doc):
    repo = _repo(tmp_path, doc)
    (repo / "renovate.json").write_text(json.dumps(_RENOVATE))
    (repo / "d" / "v.yml").write_text("app_image: org/app:1.2\napp_port: 80\n")
    git(repo, "add", "-A")
    return {(f.unit, f.rule) for f in lint_sections(repo, None)}


def test_renovate_pin_is_flagged(tmp_path):
    assert ("CLAUDE.md#A", "renovate-pin") in _pinned_repo(
        tmp_path, "## A\n`d/v.yml:app_image`\n"
    )


def test_renovate_pin_file_and_unmanaged_key_are_clean(tmp_path):
    assert (
        _pinned_repo(
            tmp_path, "## A\n`d/v.yml` holds `app_image`; `d/v.yml:app_port`\n"
        )
        == set()
    )


def test_unresolved_atom_is_flagged(tmp_path):
    assert ("CLAUDE.md#A", "unresolved-atom") in _rules(
        tmp_path, "## A\n`t/m.py:NOPE`\n"
    )


def test_ambiguous_marker_is_flagged(tmp_path):
    repo = _repo(tmp_path, "## A\n`t/m.py:DECIDED: keep`\n")
    (repo / "t" / "m.py").write_text("# DECIDED: keep it\n# DECIDED: keep that\n")
    assert ("CLAUDE.md#A", "ambiguous-marker") in {
        (f.unit, f.rule) for f in lint_sections(repo, None)
    }


def test_one_way_test_is_a_warning(tmp_path):
    found = [
        f for f in lint_sections(_repo(tmp_path, "## A\n`t/test_m.py::test_a`\n"), None)
    ]
    assert [(f.rule, f.warn) for f in found] == [("one-way-test", True)]


def test_backreferenced_test_is_clean(tmp_path):
    repo = _repo(tmp_path, "## A\n`t/test_m.py::test_a`\n")
    (repo / "t" / "test_m.py").write_text(
        "def test_a():\n    # fact: CLAUDE.md#A\n    assert True\n"
    )
    assert not lint_sections(repo, None)


def test_count_as_fact_is_a_warning(tmp_path):
    found = lint_sections(_repo(tmp_path, "## A\nThere are 13 entries here.\n"), None)
    assert [(f.rule, f.warn) for f in found] == [("count-as-fact", True)]


def test_date_as_verification_is_flagged(tmp_path):
    found = lint_sections(
        _repo(tmp_path, "## A\nVerified against the tree 2026-09-17.\n"), None
    )
    assert [(f.rule, f.warn) for f in found] == [("date-as-verification", False)]


def test_duplicate_heading_is_flagged_once_per_key(tmp_path):
    found = lint_sections(
        _repo(tmp_path, "## A\n`t/m.py:LIMIT`\n\n## B\ntwo\n\n## A\nthree\n"), None
    )
    assert [(f.unit, f.rule, f.warn) for f in found] == [
        ("CLAUDE.md#A", "duplicate-heading", False)
    ]


def test_distinct_headings_are_clean(tmp_path):
    assert _rules(tmp_path, "## A\none\n\n## B\ntwo\n\n### A child\nthree\n") == set()


def test_unbalanced_fence_is_flagged_in_the_section_holding_it(tmp_path):
    doc = "## A\n```\n`t/m.py:NOPE`\n```\n\n## B\n```text\n`t/m.py:NOPE`\n"
    found = lint_sections(_repo(tmp_path, doc), None)
    assert {(f.unit, f.rule) for f in found} == {
        ("CLAUDE.md#B", "unbalanced-fence"),
        # The rule's reason, observed: the unclosed example is graded as support.
        ("CLAUDE.md#B", "unresolved-atom"),
    }


def test_balanced_fences_are_clean(tmp_path):
    doc = "## A\n```\n`t/m.py:NOPE`\n```\ntext\n```bash\n# not a heading\n```\n"
    assert _rules(tmp_path, doc) == set()


def test_only_named_units_are_linted(tmp_path):
    repo = _repo(tmp_path, "## A\n`t/m.py:1`\n\n## B\n`t/m.py:2`\n")
    assert {f.unit for f in lint_sections(repo, {"CLAUDE.md#B"})} == {"CLAUDE.md#B"}


def test_changed_units_names_only_edited_sections(tmp_path):
    repo = _repo(tmp_path, "## A\none\n\n## B\ntwo\n")
    git(repo, "commit", "-q", "-m", "i", "--no-gpg-sign")
    (repo / "CLAUDE.md").write_text("## A\none\n\n## B\ntwo changed\n")
    assert changed_units(repo, "HEAD") == {"CLAUDE.md#B"}


def test_changed_units_refuses_an_unresolvable_ref(tmp_path):
    repo = _repo(tmp_path, "## A\none\n")
    with pytest.raises(ValueError, match="cannot resolve 'origin/master'"):
        changed_units(repo, "origin/master")


def test_a_rule_outside_the_census_is_flagged():
    """Every LintFinding above is the clean side: the census must bind its producers too."""
    with pytest.raises(ValueError, match="RULES"):
        LintFinding("CLAUDE.md#A", "nope", "d", False)


def test_rule_census():
    assert RULES == frozenset(
        {
            "rejected-form",
            "dir-without-slash",
            "unresolved-atom",
            "ambiguous-marker",
            "one-way-test",
            "lock-tampered",
            "count-as-fact",
            "date-as-verification",
            "duplicate-heading",
            "unbalanced-fence",
            "renovate-pin",
            "retired-host",
        }
    )
    assert WARN_RULES == frozenset({"count-as-fact", "one-way-test"})


def test_hand_edited_lock_is_flagged(tmp_path):
    """The clean side (no lock file) is already covered by every other test above."""
    repo = _repo(tmp_path, "## A\n`t/m.py:LIMIT`\n")
    write_lock(
        repo / LOCK_REL,
        {"CLAUDE.md#A": {"verified_sha": "abc", "atoms": {"t/m.py:LIMIT": "h"}}},
    )
    p = repo / LOCK_REL
    p.write_text(p.read_text().replace('"h"', '"hh"'))
    found = {(f.unit, f.rule): f.warn for f in lint_sections(repo, None)}
    assert found[("", "lock-tampered")] is False


# ── retired-host ─────────────────────────────────────────────────────────────────────────

# The two sentences that still described daniel-stage at 8181ee59f, twelve days after
# 8adf98ecc retired it. Both sections graded IN, because no atom they cite had moved.
_K3S_PRE_FIX = """# `setup/k3s` — the host plane

The role that turns a host into a k3s node. `daniel-box` is
the server, `daniel-server` an agent (`tasks/agent.yml`), `daniel-stage` the
staging guest whose `host_vars` turn the backup targets and the health crons off, because both
would push to prod's Kuma and B2.
"""
_DEPLOY_UI_PRE_FIX = """## At a glance
Authelia policy: `auth_tier: two_factor` on the containers_list entry.
Kept out of `STAGING_SUBSET`: daniel-stage has no daemon to route to.
"""


def _host_findings(tmp_path, **docs):
    """``retired-host`` findings over ``docs``, against a copy of the REAL inventory."""
    repo = _repo(tmp_path, docs.pop("CLAUDE.md", "## A\nnothing\n"))
    for rel, text in {
        INVENTORY_REL: (REPO / INVENTORY_REL).read_text(),
        **docs,
    }.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "-A")
    return {
        (f.unit, f.detail.split("`")[1], f.warn)
        for f in lint_sections(repo, None)
        if f.rule == "retired-host"
    }


def test_the_real_inventory_yields_the_three_hosts_and_their_prefix():
    hosts = frozenset(host_names(REPO / INVENTORY_REL))
    assert hosts == {"daniel-box", "daniel-server", "daniel-pi"}
    rx = host_pattern(hosts)
    assert rx is not None and r"daniel\-[a-z0-9]+" in rx.pattern


def test_retired_host_is_flagged(tmp_path):
    docs = {"k3s/CLAUDE.md": _K3S_PRE_FIX, "deploy-ui/CLAUDE.md": _DEPLOY_UI_PRE_FIX}
    assert _host_findings(tmp_path, **docs) == {
        ("k3s/CLAUDE.md#`setup/k3s` — the host plane", "daniel-stage", False),
        ("deploy-ui/CLAUDE.md#At a glance", "daniel-stage", False),
    }


@pytest.mark.parametrize(
    "line",
    ["run it on daniel-stage.", "`-e target=daniel-stage`", "**daniel-stage** is gone"],
)
def test_retired_host_in_any_host_position_is_flagged(tmp_path, line):
    assert _host_findings(tmp_path, **{"CLAUDE.md": f"## A\n{line}\n"}) == {
        ("CLAUDE.md#A", "daniel-stage", False)
    }


def test_live_hosts_and_host_shaped_identifiers_are_clean(tmp_path):
    doc = (
        "## A\nOn daniel-box. `-e target=daniel-pi` and **daniel-server**.\n"
        "The site is www.daniel-hunter.com, and daniel-hunter.com/x too.\n"
        "It writes `/srv/artifacts/daniel-box-claude` and `host_vars/daniel-pi.yml`.\n"
        "A retired name inside a path: `/srv/daniel-stage-old/` and `vm-daniel-stage`.\n"
    )
    assert _host_findings(tmp_path, **{"CLAUDE.md": doc}) == set()


def test_retired_host_in_a_history_bullet_is_clean(tmp_path):
    doc = (
        "## A\n- **HISTORY — the staging guest, retired 2026-09-28.** It lived on\n"
        "  daniel-stage, a KVM guest on daniel-server.\n- daniel-box is the server.\n"
    )
    assert _host_findings(tmp_path, **{"CLAUDE.md": doc}) == set()
