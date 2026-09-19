"""The closed citation grammar: a backticked span is support, rejected, or not a citation."""

import subprocess

import pytest

from lib.facts.citations import (
    FORMS,
    REJECT_REASONS,
    Citation,
    Rejected,
    Section,
    in_tree,
    parse_citations,
    repo_docs,
    sections,
    tracked_files,
)

_DOC = """intro `scripts/lib/git.py`

## Alpha
alpha text `scripts/lib/gh.py`

### Alpha child
child text

## Beta
beta text
"""


def _one(text):
    cites, rejects = parse_citations(text)
    assert len(cites) + len(rejects) == 1, (cites, rejects)
    return (cites or rejects)[0]


def test_path_file_is_clean():
    c = _one("see `scripts/lib/kubectl.py` for the gate")
    assert c == Citation("path", "scripts/lib/kubectl.py", "scripts/lib/kubectl.py", "")


def test_path_directory_keeps_trailing_slash():
    c = _one("under `ansible/roles/k8s/traefik/`")
    assert c.form == "path" and c.path == "ansible/roles/k8s/traefik/"


def test_symbol_is_clean():
    c = _one("`scripts/lib/kubectl.py:run` refuses")
    assert c == Citation(
        "symbol", "scripts/lib/kubectl.py:run", "scripts/lib/kubectl.py", "run"
    )


def test_yaml_key_is_clean():
    c = _one("`ansible/roles/k8s/traefik/defaults/main.yml:traefik_replicas` is 2")
    assert c.form == "yaml"
    assert c.path == "ansible/roles/k8s/traefik/defaults/main.yml"
    assert c.selector == "traefik_replicas"


def test_yaml_dotted_key_is_clean():
    c = _one("`ansible/inventory/host_vars/daniel-box.yml:containers_list.0.name`")
    assert c.form == "yaml" and c.selector == "containers_list.0.name"


def test_test_node_is_clean():
    c = _one(
        "ENFORCED by `scripts/lib/tests/test_kubectl.py::test_refuses_other_cluster`"
    )
    assert c.form == "test"
    assert c.path == "scripts/lib/tests/test_kubectl.py"
    assert c.selector == "test_refuses_other_cluster"


def test_marker_is_clean():
    c = _one(
        "`ansible/roles/setup/gitops_deploy/files/deploy_logic.py:DECIDED: a fixed slice while`"
    )
    assert c.form == "marker"
    assert c.selector == "a fixed slice while"


def test_probe_is_clean():
    c = _one("`probe.py kuma-drift` answers what is missing")
    assert c == Citation("probe", "probe.py kuma-drift", "", "kuma-drift")


def test_probe_with_argument_is_clean():
    c = _one("`probe.py health traefik`")
    assert c.selector == "health traefik"


def test_file_line_is_flagged():
    r = _one("`deploy_logic.py:458` used to say")
    assert r == Rejected("deploy_logic.py:458", "file:line")


def test_file_line_range_is_flagged():
    assert _one("`CLAUDE.md:12-20`").reason == "file:line"


def test_bare_word_is_not_a_citation():
    assert parse_citations("run `sops` then `--dry-run` and `kubectl apply`") == (
        [],
        [],
    )


def test_bare_filename_without_slash_is_not_a_citation():
    assert parse_citations("`docker-compose.yml` is rendered") == ([], [])


def test_host_port_is_not_a_citation():
    assert parse_citations("listens on `127.0.0.1:3100`") == ([], [])


def test_fenced_code_is_skipped():
    text = "```\n`scripts/lib/git.py`\n```\nand `scripts/lib/gh.py`"
    cites, _ = parse_citations(text)
    assert [c.raw for c in cites] == ["scripts/lib/gh.py"]


def test_form_census():
    assert FORMS == frozenset({"path", "symbol", "yaml", "test", "marker", "probe"})
    assert REJECT_REASONS == frozenset({"file:line"})


@pytest.mark.parametrize("raw", ["scripts/lib/kubectl.py:run", "probe.py kuma-drift"])
def test_every_citation_round_trips_its_raw(raw):
    assert _one(f"`{raw}`").raw == raw


def test_sections_are_keyed_by_doc_and_heading():
    got = sections("CLAUDE.md", _DOC)
    assert [s.key for s in got] == [
        "CLAUDE.md#",
        "CLAUDE.md#Alpha",
        "CLAUDE.md#Alpha child",
        "CLAUDE.md#Beta",
    ]


def test_a_section_body_ends_at_the_next_heading_of_any_level():
    alpha = sections("CLAUDE.md", _DOC)[1]
    assert "alpha text" in alpha.body
    assert "child text" not in alpha.body
    assert "beta text" not in alpha.body


def test_a_child_section_holds_only_its_own_text():
    child = sections("CLAUDE.md", _DOC)[2]
    assert child == Section("CLAUDE.md#Alpha child", "Alpha child", "child text\n\n")


def test_a_hash_line_inside_a_fence_is_not_a_heading():
    doc = """## A
fenced code block:
```bash
# not a heading
```
## B
"""
    got = sections("CLAUDE.md", doc)
    assert [s.key for s in got] == ["CLAUDE.md#A", "CLAUDE.md#B"]
    assert "# not a heading" in got[0].body


def _init(tmp_path):
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "master", str(tmp_path)], check=True, env=env
    )
    return env


def test_tracked_files_lists_only_tracked(tmp_path):
    env = _init(tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_text("x\n")
    (tmp_path / "a" / "untracked.txt").write_text("y\n")
    subprocess.run(["git", "add", "a/x.txt"], cwd=tmp_path, check=True, env=env)
    assert tracked_files(tmp_path) == frozenset({"a/x.txt"})


def test_in_tree_tracked_file_is_clean(tmp_path):
    env = _init(tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_text("x\n")
    subprocess.run(["git", "add", "a/x.txt"], cwd=tmp_path, check=True, env=env)
    tracked = tracked_files(tmp_path)
    assert in_tree(Citation("path", "a/x.txt", "a/x.txt", ""), tracked)


def test_in_tree_directory_with_a_tracked_member_is_clean(tmp_path):
    env = _init(tmp_path)
    (tmp_path / "a" / "sub").mkdir(parents=True)
    (tmp_path / "a" / "sub" / "y.txt").write_text("y\n")
    subprocess.run(["git", "add", "a"], cwd=tmp_path, check=True, env=env)
    tracked = tracked_files(tmp_path)
    assert in_tree(Citation("path", "a/sub/", "a/sub/", ""), tracked)


def test_in_tree_untracked_file_is_flagged(tmp_path):
    _init(tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_text("x\n")
    # Never `git add`ed: it exists on disk but not in the index.
    tracked = tracked_files(tmp_path)
    assert not in_tree(Citation("path", "a/x.txt", "a/x.txt", ""), tracked)


def test_in_tree_gitignored_file_is_flagged(tmp_path):
    env = _init(tmp_path)
    (tmp_path / ".gitignore").write_text("a/ignored.md\n")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "ignored.md").write_text("spec\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True, env=env)
    tracked = tracked_files(tmp_path)
    assert not in_tree(Citation("path", "a/ignored.md", "a/ignored.md", ""), tracked)


def test_in_tree_out_of_tree_token_is_flagged():
    tracked = frozenset({"ansible/roles/k8s/traefik/defaults/main.yml"})
    out_of_tree = [
        Citation("path", "origin/master", "origin/master", ""),
        Citation("path", "10.42.0.0/16", "10.42.0.0/16", ""),
    ]
    assert [c.raw for c in out_of_tree if in_tree(c, tracked)] == []


def test_probe_is_always_in_tree():
    # A probe citation carries no path at all, so the tracked-set test cannot apply to it.
    assert in_tree(
        Citation("probe", "probe.py kuma-drift", "", "kuma-drift"), frozenset()
    )


def test_repo_docs_lists_every_tracked_claude_md(tmp_path):
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "master", str(tmp_path)], check=True, env=env
    )
    (tmp_path / "CLAUDE.md").write_text("# root\n")
    (tmp_path / "role").mkdir()
    (tmp_path / "role" / "CLAUDE.md").write_text("# role\n")
    (tmp_path / "role" / "README.md").write_text("not a store\n")
    (tmp_path / "untracked").mkdir()
    (tmp_path / "untracked" / "CLAUDE.md").write_text("# not added\n")
    subprocess.run(
        ["git", "add", "CLAUDE.md", "role"], cwd=tmp_path, check=True, env=env
    )
    assert repo_docs(tmp_path) == [
        tmp_path / "CLAUDE.md",
        tmp_path / "role" / "CLAUDE.md",
    ]
