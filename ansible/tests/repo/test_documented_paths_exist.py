"""Every `file:line` citation in the operator docs must point at a file that exists.

Prose asserts facts that nothing re-derives, so a rename leaves the docs pointing somewhere
that used to be right. `test_documented_macros_exist.py` covers that for shared macro names;
this covers it for the paths themselves, which that guard's `*.yml.j2` pattern cannot see.

The evidence is standing. The 2026-08-14 k3s migration moved eleven roles out of
`roles/containers/`, and later passes moved most of `scripts/` into subdirectories. A session
opened on 2026-08-27 was told five of its memory files still named paths that had moved,
`scripts/gen_infra_map.py` (now under `scripts/infra_map/`) among them. Memory files get that
check at session start; the repo's own docs get none, and a doc is what an agent reads before
it edits. A stale citation still reads as an instruction: the reader opens it, finds nothing,
and either guesses or gives up.

**Only citations carrying a line number count.** That restriction is the whole design, and it
was arrived at by measuring. A bare-path version of this guard flagged 18 places, and every
one of them was a path that was never a claim about this tree at a moment in time: a role's
own `docs/platform.md` read from a doc that lives elsewhere, `.github/workflows/image.yml`
inside the *upstream* texbrain fork, `ansible/roles/k8s/anilist-tags` in a design doc for a
role nobody has built yet, a retired Kopia procedure, and `.claude/worktrees/` -- which exists
in the primary checkout and not in a fresh worktree, so it made the verdict depend on where
the suite ran. Nobody writes `file.py:412` about a planned file, another repo's file, or a
runtime directory. A line number is a claim that this exact file exists in this tree right
now, which is exactly the claim that rots silently on a rename.

That is why there is no allowlist here. Every exclusion a bare-path version needed was a
narrowing someone would later have to defend, and an allowlist is where a real finding goes
to hide. The pattern earns the zero-entry list.

Two more citation shapes are checked here, each with its own extractor and paired tests. A
bare `ansible/tests/...py` path -- that directory exists nowhere but this tree, so a bare
citation of it is still a claim about this tree now (`_CITED_TEST`). And a `file.py::test_name`
node id, in docs AND in runtime modules, which names the check that holds an invariant closed
(`_CITED_NODE`). They lived in this file and in `test_cited_tests_exist.py` until 2026-09-02;
one corpus walk and one tracked-file set serve all three.
"""

import re
import subprocess
from pathlib import Path

import pytest
from _helpers import REPO, discover_docs


# A floor, not the real count -- catches the walk silently shrinking (a renamed root, a
# tightened exclude) without hardcoding a number that drifts every time a doc is added.
# Set just under the ~166 the walk finds today: far enough below to absorb ordinary
# deletions, close enough that a broken walk cannot pass.
_MIN_DOCS = 100

# A backticked filename carrying an explicit line reference. The `:NNN` is the load-bearing
# part -- see the module docstring.
#
# DECIDED: no repo-root prefix is required. Requiring one ("ansible/...", "scripts/...")
# matched 4 citations in the whole tree, because the docs overwhelmingly cite from context --
# `roles/k8s/manifests/tasks/main.yml:112`, `deploy_logic.py:458`. A guard that checks four
# things is a guard that passes vacuously, which is why the count assertion below exists.
# The extension must begin with a letter, which is what keeps `127.0.0.1:3100` and
# `10.0.0.240:51820` from parsing as a file called `127.0.0.` with extension `1`. Four role
# docs and two networking docs cite host:port pairs exactly that way.
_CITED = re.compile(r"`([\w.][\w./-]*\.[a-z][a-z0-9]*):(\d+)(?:-\d+)?`")


DOCS = discover_docs()


def _repo_files() -> set[str]:
    """Every TRACKED file path in the repo, repo-relative, as forward-slash strings.

    DECIDED: `git ls-files`, not an `rglob` plus a skip list. An rglob sees whatever happens
    to be on disk, and this repo grows untracked trees during ordinary work: `.venv`,
    `ansible/collections/` (vendored per worktree), `__pycache__`, and `styles/`, which
    `vale sync` creates and which carries `.txt` and `.json` files the repo itself does not
    have. That last one moves `_REPO_EXTENSIONS` below, so the same citation would be checked
    on a synced checkout and skipped on a fresh one. A guard whose verdict depends on whether
    someone has run `vale sync` has the defect this module rejected `.claude/worktrees/` for.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return {p for p in listed.stdout.split("\0") if p}


REPO_FILES = _repo_files()

# Extensions this repo actually contains. A citation in some other extension is a file in
# some other repository -- `docs/longhorn-backup-tiering.md` cites Longhorn's own
# `deltablock.go:117` and `s3.go:88` to show where upstream does the thing. Deriving the set
# from the tree means a language this repo adopts later is covered without an edit here,
# rather than being silently exempt.
_REPO_EXTENSIONS = {Path(p).suffix.lstrip(".") for p in REPO_FILES}


def resolves(cited: str, doc: Path) -> bool:
    """Whether a cited path names a file that exists, the way a reader would resolve it.

    Doc-relative first, then repo-relative, then as a path SUFFIX of some real file -- which
    is what makes a context-relative citation like `roles/k8s/manifests/tasks/main.yml:112`
    resolve without the `ansible/` prefix nobody writes.

    DECIDED: a suffix, never a bare basename. `scripts/gen_infra_map.py` is not a suffix of
    `scripts/infra_map/gen_infra_map.py`, so the move that prompted this guard still fails it;
    matching on basename alone would have accepted it and made the guard decorative.
    """
    if (doc.parent / cited).exists() or (REPO / cited).exists():
        return True
    tail = "/" + cited
    return any(known.endswith(tail) for known in REPO_FILES)


def cited_paths(line: str) -> list[str]:
    """Every line-numbered repo path cited in one line of prose.

    Split out from the walk so the pattern can be exercised against inputs it must accept AND
    inputs it must reject. A guard that matches everything and one that matches nothing are
    indistinguishable from the passing side alone.
    """
    return [
        path
        for path, _line_no in _CITED.findall(line)
        if Path(path).suffix.lstrip(".") in _REPO_EXTENSIONS
    ]


# A bare `ansible/tests/<file>.py` citation is the ONE exception to the line-number rule. Every
# such path is a claim about this tree: nothing else has a directory by that name, and a
# `pytest <path>` line or an `ENFORCED by <path>` note is an instruction someone runs. The
# 2026-09-01 move of the guards into subdirectories rewrote 305 of these, and the line-number
# rule would have watched none of them go stale. A trailing `::node_id` is allowed and dropped.
_CITED_TEST = re.compile(
    r"`(ansible/tests/[\w./-]+\.py)(?::\d+(?:-\d+)?|::[\w\[\]:.-]+)?`"
)

# Archived plans describe the tree at the moment they were executed, so a test they name may
# since have been retired with the work it guarded. Live docs are read as instructions.
_ARCHIVE = "docs/archive/"


def cited_test_paths(line: str) -> list[str]:
    """Every `ansible/tests/...py` cited in one line of prose, with or without a line number."""
    return _CITED_TEST.findall(line)


# --- the extractor's own paired tests -------------------------------------------------


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
def test_a_line_numbered_citation_is_extracted(line, expected):
    assert cited_paths(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        # The whole point of the design: a bare path is not a claim about this tree now.
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
        # Upstream Longhorn source, in another repository entirely.
        "upstream does it in `deltablock.go:117`",
        "and `s3.go:88` for the store half",
    ],
)
def test_a_non_citation_is_not_extracted(line):
    assert cited_paths(line) == []


def test_resolution_accepts_a_context_relative_citation():
    """The common shape: the docs cite from context, without the `ansible/` prefix."""
    doc = REPO / "CLAUDE.md"
    assert resolves("roles/k8s/manifests/tasks/main.yml", doc)
    assert resolves("ansible/roles/k8s/manifests/tasks/main.yml", doc)


def test_resolution_rejects_a_path_that_moved():
    """The proof this guard can go red, using the rename that prompted it.

    `scripts/gen_infra_map.py` moved to `scripts/infra_map/gen_infra_map.py`. Matching on
    basename would accept the old path and make the whole guard decorative, so this pins the
    suffix rule at the one case that distinguishes them.
    """
    doc = REPO / "CLAUDE.md"
    assert (REPO / "scripts" / "infra_map" / "gen_infra_map.py").exists(), (
        "the fixture moved; pick another renamed file to pin the suffix rule"
    )
    assert not resolves("scripts/gen_infra_map.py", doc)
    assert resolves("scripts/infra_map/gen_infra_map.py", doc)


# --- the guard itself -----------------------------------------------------------------


def test_the_corpus_covers_the_whole_doc_tree():
    """Coverage is asserted, not assumed -- the macro guard shipped reading two files."""
    assert len(DOCS) >= _MIN_DOCS, (
        f"only found {len(DOCS)} docs, expected at least {_MIN_DOCS} -- the walk shrank. "
        "A floor far below the real count cannot tell 'the walk broke' from 'docs were "
        "deleted'."
    )


def test_the_guard_finds_citations_to_check():
    """A pattern that silently stopped matching would pass the guard below vacuously."""
    hits = sum(
        len(cited_paths(line)) for doc in DOCS for line in doc.read_text().splitlines()
    )
    # 30 today. A floor just under it catches the pattern breaking; setting it at the exact
    # count would instead fail every time a doc drops a citation, which is not a defect.
    assert hits >= 25, (
        f"only {hits} line-numbered citations found across {len(DOCS)} docs -- the pattern "
        "has stopped matching, so the existence check below is passing on an empty set."
    )


def test_every_line_numbered_path_cited_in_the_docs_exists():
    missing = []
    for doc in DOCS:
        if not doc.is_file():
            continue
        for line_no, line in enumerate(doc.read_text().splitlines(), 1):
            for cited in cited_paths(line):
                if resolves(cited, doc):
                    continue
                rel = doc.relative_to(REPO)
                missing.append(f"{rel}:{line_no} cites {cited}")

    assert not missing, (
        "the docs cite a repo path that does not exist; an agent following one of these "
        "opens nothing and then guesses:\n  " + "\n  ".join(missing)
    )


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
def test_a_bare_test_citation_is_extracted(line, expected):
    assert cited_test_paths(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "the guards live under `ansible/tests/`",
        "see `scripts/deploy.sh` for the wrapper",
        "a role's own `tests/test_macros.py`",
        "ansible/tests/repo/test_helpers.py with no code span",
    ],
)
def test_a_non_test_citation_is_not_extracted(line):
    assert cited_test_paths(line) == []


def test_every_test_path_cited_in_the_live_docs_exists():
    missing = []
    for doc in DOCS:
        rel = doc.relative_to(REPO)
        if not doc.is_file() or str(rel).startswith(_ARCHIVE):
            continue
        for line_no, line in enumerate(doc.read_text().splitlines(), 1):
            for cited in cited_test_paths(line):
                if cited in REPO_FILES:
                    continue
                missing.append(f"{rel}:{line_no} cites {cited}")

    assert not missing, (
        "the docs cite a test file that does not exist at that path; `pytest <path>` on any "
        "of these collects nothing:\n  " + "\n  ".join(missing)
    )


def test_the_test_citation_walk_finds_citations_to_check():
    """The move that motivated this check touched 118 live citations; a shrunken walk is a bug."""
    found = sum(
        len(cited_test_paths(line))
        for doc in DOCS
        if doc.is_file() and not str(doc.relative_to(REPO)).startswith(_ARCHIVE)
        for line in doc.read_text().splitlines()
    )
    assert found >= 50, found


# --- `file.py::test_name` node ids -----------------------------------------------------
#
# A runtime module that says "pinned by test_x.py::test_the_thing" is telling the next reader
# which check holds an invariant closed. That claim rots the way a `file:line` citation does,
# and neither pattern above sees it: a node id carries no line number, and most are cited
# from context (`test_check_streaks.py::...` inside the same directory) rather than from the
# repo root. The evidence is the 2026-09-01 test split (PR #744): nine citations across three
# runtime modules, two docs and an ADR named files that no longer existed, and only the one
# carrying a line number was caught. This corpus is wider than DOCS because the citation
# lives in two places: docs cite from prose with backticks, runtime modules from a docstring
# or a comment with none.
#
# The path half must end in `.py`, which keeps `host:port` and `key::value` prose out; the
# test half must start with `test_`, so a fixture cited by node id (`conftest.py::seq`) is
# not a claim this checks.
_CITED_NODE = re.compile(r"([\w.][\w./-]*\.py)::(test_\w+)")


def _runtime_modules() -> list[Path]:
    """Every tracked first-party runtime .py file.

    The vendored collections tree is nobody's claim. Test files are out too: a string inside
    a test is an input, not a claim -- this module carries
    `ansible/tests/k8s/test_x.py::test_the_thing` as a parametrized fixture.
    """
    return [
        REPO / p
        for p in REPO_FILES
        if p.endswith(".py")
        and not p.startswith("ansible/collections/")
        and not Path(p).name.startswith("test_")
        and Path(p).name != "conftest.py"
    ]


NODE_CORPUS = sorted(set(DOCS) | set(_runtime_modules()))


def cited_tests(line: str) -> list[tuple[str, str]]:
    """Every (path, test_name) pair cited in one line."""
    return _CITED_NODE.findall(line)


def candidates(cited: str, source: Path) -> list[Path]:
    """The tracked files a cited path could mean, resolved the way `resolves` above does.

    Relative to the citing file, to the repo root, or as a path suffix of a tracked file.
    """
    found = [d for d in (source.parent / cited, REPO / cited) if d.is_file()]
    tail = "/" + cited
    found.extend(REPO / known for known in REPO_FILES if known.endswith(tail))
    return found


def defines_test(path: Path, name: str) -> bool:
    return (
        re.search(rf"^\s*(?:async\s+)?def {re.escape(name)}\(", path.read_text(), re.M)
        is not None
    )


def node_resolves(cited: str, name: str, source: Path) -> bool:
    return any(defines_test(p, name) for p in candidates(cited, source))


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
    ],
)
def test_a_node_citation_is_extracted(line, expected):
    assert cited_tests(line) == expected


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
def test_a_non_node_citation_is_not_extracted(line):
    assert cited_tests(line) == []


def test_node_resolution_accepts_a_context_relative_citation():
    """A runtime module citing its sibling test by bare filename, the common shape."""
    source = REPO / "ansible/roles/setup/gitops_deploy/files/deploy_k8s.py"
    assert node_resolves(
        "test_deploy_k8s_declarations.py",
        "test_declares_snapshot_claims_agrees_with_yaml_for_every_k8s_role",
        source,
    )


def test_node_resolution_rejects_a_renamed_test_and_a_renamed_file():
    """The proof this guard can go red.

    The file the 2026-09-01 split retired, and a test name nothing defines. Matching on the file
    alone would accept the second.
    """
    source = REPO / "ansible/roles/setup/gitops_deploy/files/deploy_k8s.py"
    assert not node_resolves(
        "test_deploy_logic.py",
        "test_declares_snapshot_claims_agrees_with_yaml_for_every_k8s_role",
        source,
    )
    assert not node_resolves(
        "test_deploy_k8s_declarations.py", "test_no_such_test_anywhere", source
    )


def test_the_node_walk_finds_citations_to_check():
    """A pattern that silently stopped matching would pass the guard below vacuously."""
    hits = sum(
        len(cited_tests(line))
        for path in NODE_CORPUS
        for line in path.read_text(errors="replace").splitlines()
    )
    # 10 today. A floor just under it catches the pattern breaking without failing every
    # time a doc drops a citation.
    assert hits >= 8, (
        f"only {hits} test citations found across {len(NODE_CORPUS)} files -- the pattern "
        "has stopped matching, so the existence check below is passing on an empty set."
    )


def test_every_cited_test_exists():
    missing = []
    for path in NODE_CORPUS:
        if not path.is_file():
            continue
        for line_no, line in enumerate(
            path.read_text(errors="replace").splitlines(), 1
        ):
            for cited, name in cited_tests(line):
                if node_resolves(cited, name, path):
                    continue
                missing.append(
                    f"{path.relative_to(REPO)}:{line_no} cites {cited}::{name}"
                )

    assert not missing, (
        "a doc or module cites a test that no file defines, so the invariant it says is "
        "pinned may be pinned by nothing:\n  " + "\n  ".join(missing)
    )


def test_the_walk_ignores_other_worktrees_on_disk():
    """The regression: `.claude/worktrees/<name>/` is a full checkout per live session.

    An rglob walked into those and judged this commit against other sessions' older docs, so
    the guard failed on paths that had moved perfectly legitimately. It broke the docs-refresh
    cron — its commit runs the prek hooks, this guard rejected it, and the script took its
    "commit failed; unstaged, nothing published" path while master CI stayed green, because a
    CI runner has no worktrees on disk. Found 2026-08-27.
    """
    # Anchored to REPO, not matched as a substring: this suite often RUNS from inside
    # `.claude/worktrees/<name>/`, so every legitimate doc path contains that text. The
    # question is whether the walk left THIS checkout, which only the parent test answers.
    nested = REPO / ".claude" / "worktrees"
    strays = [d for d in DOCS if nested in d.parents]
    assert not strays, "the doc walk reached into another checkout: " + ", ".join(
        d.as_posix() for d in strays[:3]
    )


def test_the_walk_still_finds_this_repos_own_docs():
    """The other half: an exclusion tight enough to skip worktrees must not skip the tree.

    Without this, "ignore other checkouts" is satisfied by finding nothing at all.
    """
    names = {d.name for d in DOCS}
    assert "CLAUDE.md" in names
    assert any(d.as_posix().endswith("docs/secret-rotation.md") for d in DOCS)
