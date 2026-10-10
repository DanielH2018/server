"""The closed citation grammar: a backticked span is support, rejected, or not a citation."""

import pytest

from lib.git_testing import git, init_repo
from lib.facts.citations import (
    FORMS,
    HISTORY_MARKER,
    REJECT_REASONS,
    Citation,
    Rejected,
    Section,
    guard_path_citations,
    in_tree,
    line_numbered_citations,
    macro_citations,
    mask_history,
    node_citations,
    parse_citations,
    repo_docs,
    sections,
    spans,
    tracked_files,
)
from lib.repo_paths import REPO

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


@pytest.mark.parametrize(
    "line,expected",
    [
        (
            "pinned by `ansible/tests/k8s/test_x.py::test_the_thing`",
            [("ansible/tests/k8s/test_x.py", "test_the_thing")],
        ),
        (
            "    test_deploy_k8s_declarations.py::test_declares_snapshot_claims_agrees,",
            [
                (
                    "test_deploy_k8s_declarations.py",
                    "test_declares_snapshot_claims_agrees",
                )
            ],
        ),
        (
            "both a.py::test_a and `b/c.py::test_b` here",
            [("a.py", "test_a"), ("b/c.py", "test_b")],
        ),
        (
            "```\n`scripts/lib/tests/test_kubectl.py::test_run`\n```",
            [("scripts/lib/tests/test_kubectl.py", "test_run")],
        ),
    ],
)
def test_a_prose_node_citation_is_clean(line, expected):
    assert node_citations(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "see `scripts/example.sh` for the wrapper",
        "at `ansible/roles/k8s/sonarr/tasks/main.yml:12`",
        "the collector listens on `127.0.0.1:4317`",
        "a fixture, `conftest.py::seq`, not a test",
        "a C++ scope `ns::test_helper` is not a file",
        "plain prose with no citation at all",
    ],
)
def test_a_non_node_span_is_flagged_as_no_citation(line):
    assert node_citations(line) == []


_EXTENSIONS = frozenset({"md", "py", "sh", "yml"})


@pytest.mark.parametrize(
    "line,expected",
    [
        (
            "at `ansible/roles/k8s/sonarr/tasks/main.yml:12`",
            ["ansible/roles/k8s/sonarr/tasks/main.yml"],
        ),
        (
            "spans `scripts/dev/prune_worktrees.py:10-40`.",
            ["scripts/dev/prune_worktrees.py"],
        ),
        (
            "both `scripts/a.py:1` and `.claude/hooks/b.sh:22`",
            ["scripts/a.py", ".claude/hooks/b.sh"],
        ),
        ("see `docs/index.md:3` please", ["docs/index.md"]),
    ],
)
def test_a_line_numbered_citation_is_clean(line, expected):
    assert line_numbered_citations(line, _EXTENSIONS) == expected


@pytest.mark.parametrize(
    "line",
    [
        # A bare path is not a claim about this tree now.
        "see `scripts/deploy.sh` for the wrapper",
        "the `docs/adr/` series",
        "a new role, `ansible/roles/k8s/anilist-tags`, runs a CronJob",
        # A pytest node id is not a line reference.
        "pinned by `ansible/tests/test_x.py::test_the_thing`",
        # A placeholder segment is a shape, not a path.
        "edit `containers/<svc>/docker-compose.yml:4` instead",
        "run `kubectl get pods` first",
        "plain prose with no code span at all",
        # host:port, not file:line -- the numeric extension trap.
        "the collector listens on `127.0.0.1:4317`",
        "bound to `10.0.0.240:51820` on the LAN",
        "never `0.0.0.0:8080`",
        # An extension the caller's tree does not hold is another repository's file.
        "upstream does it in `deltablock.go:117`",
    ],
)
def test_a_non_line_numbered_span_is_flagged_as_no_citation(line):
    assert line_numbered_citations(line, _EXTENSIONS) == []


@pytest.mark.parametrize(
    "line,expected",
    [
        (
            "run `ansible/tests/repo/test_helpers.py` first",
            ["ansible/tests/repo/test_helpers.py"],
        ),
        (
            "pinned by `ansible/tests/k8s/test_x.py::test_the_thing`",
            ["ansible/tests/k8s/test_x.py"],
        ),
        ("ENFORCED: `ansible/tests/_helpers.py:27`", ["ansible/tests/_helpers.py"]),
    ],
)
def test_a_guard_path_citation_is_clean(line, expected):
    assert guard_path_citations(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "the guards live under `ansible/tests/`",
        "see `scripts/deploy.sh` for the wrapper",
        "a role's own `tests/test_macros.py`",
        "ansible/tests/repo/test_helpers.py with no code span",
    ],
)
def test_a_non_guard_path_span_is_flagged_as_no_citation(line):
    assert guard_path_citations(line) == []


@pytest.mark.parametrize(
    "line,expected",
    [
        (
            "{% from 'k8s_probes.yml.j2' import http_probe %}",
            ["k8s_probes.yml.j2"],
        ),
        (
            "renders `templates/config/config.yml.j2` and `traefik_labels.yml.j2`",
            ["config/config.yml.j2", "traefik_labels.yml.j2"],
        ),
    ],
)
def test_a_macro_citation_is_clean(line, expected):
    assert macro_citations(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "the role's `values.yml` and `deployment.yaml.j2`",
        "a plain `docker-compose.yml` file",
        "prose with no template name",
    ],
)
def test_a_non_macro_span_is_flagged_as_no_citation(line):
    assert macro_citations(line) == []


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


def test_a_trailing_atx_closer_is_not_part_of_the_heading():
    got = sections("CLAUDE.md", "## Alpha ##\ntext\n\n## Beta #\n\n## Issue #3\n")
    assert [s.key for s in got] == [
        "CLAUDE.md#Alpha",
        "CLAUDE.md#Beta",
        "CLAUDE.md#Issue #3",
    ]
    assert got[0].heading == "Alpha" and got[0].body == "text\n\n"


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
    init_repo(tmp_path)


def test_tracked_files_lists_only_tracked(tmp_path):
    _init(tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_text("x\n")
    (tmp_path / "a" / "untracked.txt").write_text("y\n")
    git(tmp_path, "add", "a/x.txt")
    assert tracked_files(tmp_path) == frozenset({"a/x.txt"})


def test_in_tree_tracked_file_is_clean(tmp_path):
    _init(tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_text("x\n")
    git(tmp_path, "add", "a/x.txt")
    tracked = tracked_files(tmp_path)
    assert in_tree(Citation("path", "a/x.txt", "a/x.txt", ""), tracked)


def test_in_tree_directory_with_a_tracked_member_is_clean(tmp_path):
    _init(tmp_path)
    (tmp_path / "a" / "sub").mkdir(parents=True)
    (tmp_path / "a" / "sub" / "y.txt").write_text("y\n")
    git(tmp_path, "add", "a")
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
    _init(tmp_path)
    (tmp_path / ".gitignore").write_text("a/ignored.md\n")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "ignored.md").write_text("spec\n")
    git(tmp_path, "add", ".gitignore")
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
    init_repo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# root\n")
    (tmp_path / "role").mkdir()
    (tmp_path / "role" / "CLAUDE.md").write_text("# role\n")
    (tmp_path / "role" / "README.md").write_text("not a store\n")
    (tmp_path / "untracked").mkdir()
    (tmp_path / "untracked" / "CLAUDE.md").write_text("# not added\n")
    git(tmp_path, "add", "CLAUDE.md", "role")
    assert repo_docs(tmp_path) == [
        tmp_path / "CLAUDE.md",
        tmp_path / "role" / "CLAUDE.md",
    ]


def test_the_real_wg_easy_history_bullet_is_masked_and_its_neighbours_are_not():
    """The bullet that set the convention. Its continuation lines go with it; the next stays."""
    text = (REPO / "ansible/roles/containers/wg-easy/CLAUDE.md").read_text()
    assert "\n- " + HISTORY_MARKER in text
    masked = mask_history(text)
    assert "wg_easy_password_hash` no longer exists" not in masked
    assert "Exposure is LAN-bound" in masked
    assert "Pi UI is unauthenticated" in masked
    assert masked.count("\n") == text.count("\n")


def test_a_history_paragraph_ends_at_the_blank_line():
    text = f"{HISTORY_MARKER} gone.** It used `x_y`.\nStill `x_y`.\n\nLive `a_b`.\n"
    assert spans(mask_history(text)) == ["a_b"]


def test_a_history_bullet_keeps_its_nested_bullets_and_ends_at_a_sibling():
    text = (
        f"- {HISTORY_MARKER} gone.**\n\n  - nested `x_y`\n- sibling `a_b`\n"
        "  - its child `c_d`\n"
    )
    assert spans(mask_history(text)) == ["a_b", "c_d"]


def test_the_marker_mid_sentence_is_prose():
    text = f"See the {HISTORY_MARKER} bullet.** `a_b`\n- the {HISTORY_MARKER}** `c_d`\n"
    assert mask_history(text) == text
