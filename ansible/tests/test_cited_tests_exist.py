"""Every `file.py::test_name` citation in the docs and the source must name a test that exists.

A runtime module that says "pinned by test_x.py::test_the_thing" is telling the next reader
which check holds an invariant closed. That claim rots the same way a `file:line` citation
does, and `test_documented_paths_exist.py` deliberately ignores it -- a pytest node id is not
a line reference. The evidence is the 2026-09-01 test split (PR #744): nine citations across
three runtime modules, two docs and an ADR named files that no longer existed, and only the
one carrying a line number was caught.

Two corpora, because the citation lives in two places. Docs cite from prose with backticks;
runtime modules cite from a docstring or a comment with none. The pattern accepts both, and
the file half resolves the way `test_documented_paths_exist.resolves` does, so a citation
from context (`test_check_streaks.py::...` inside the same directory) is found by suffix.

Run: uv run pytest ansible/tests/test_cited_tests_exist.py
"""

import re
import subprocess
from pathlib import Path

import pytest
from _helpers import discover_docs

REPO = Path(__file__).resolve().parent.parent.parent

# `<path>.py::test_<name>`, backticked or bare. The path half must end in `.py`, which is
# what keeps `host:port` and `key::value` prose out; the test half must start with `test_`,
# so a fixture or helper cited by node id (`conftest.py::seq`) is not a claim this checks.
_CITED = re.compile(r"([\w.][\w./-]*\.py)::(test_\w+)")


def _tracked_python() -> list[Path]:
    """Every tracked first-party runtime .py file.

    The vendored collections tree is nobody's claim. Test files are out too: a string inside
    a test is an input, not a claim -- `test_documented_paths_exist.py` carries
    `ansible/tests/test_x.py::test_the_thing` as a parametrized fixture, and this file
    carries its own.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return [
        REPO / p
        for p in listed.stdout.split("\0")
        if p
        and not p.startswith("ansible/collections/")
        and not Path(p).name.startswith("test_")
        and Path(p).name != "conftest.py"
    ]


def _tracked_files() -> set[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return {p for p in listed.stdout.split("\0") if p}


REPO_FILES = _tracked_files()
CORPUS = sorted(set(discover_docs()) | set(_tracked_python()))


def cited_tests(line: str) -> list[tuple[str, str]]:
    """Every (path, test_name) pair cited in one line."""
    return _CITED.findall(line)


def candidates(cited: str, source: Path) -> list[Path]:
    """The tracked files a cited path could mean: relative to the citing file, to the repo
    root, or as a path suffix of a tracked file. A suffix, never a bare basename -- the
    reasoning is `test_documented_paths_exist.resolves`'s and is not repeated here."""
    found = []
    for direct in (source.parent / cited, REPO / cited):
        if direct.is_file():
            found.append(direct)
    tail = "/" + cited
    found.extend(REPO / known for known in REPO_FILES if known.endswith(tail))
    return found


def defines_test(path: Path, name: str) -> bool:
    return (
        re.search(rf"^\s*(?:async\s+)?def {re.escape(name)}\(", path.read_text(), re.M)
        is not None
    )


def resolves(cited: str, name: str, source: Path) -> bool:
    return any(defines_test(p, name) for p in candidates(cited, source))


# --- the extractor's own paired tests -------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        (
            "pinned by `ansible/tests/test_x.py::test_the_thing`",
            [("ansible/tests/test_x.py", "test_the_thing")],
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
def test_a_test_citation_is_extracted(line, expected):
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
def test_a_non_citation_is_not_extracted(line):
    assert cited_tests(line) == []


def test_resolution_accepts_a_context_relative_citation():
    """A runtime module citing its sibling test by bare filename, the common shape."""
    source = REPO / "ansible/roles/setup/gitops_deploy/files/deploy_k8s.py"
    assert resolves(
        "test_deploy_k8s_declarations.py",
        "test_declares_snapshot_claims_agrees_with_yaml_for_every_k8s_role",
        source,
    )


def test_resolution_rejects_a_renamed_test_and_a_renamed_file():
    """The proof this guard can go red: the file the 2026-09-01 split retired, and a test
    name nothing defines. Matching on the file alone would accept the second."""
    source = REPO / "ansible/roles/setup/gitops_deploy/files/deploy_k8s.py"
    assert not resolves(
        "test_deploy_logic.py",
        "test_declares_snapshot_claims_agrees_with_yaml_for_every_k8s_role",
        source,
    )
    assert not resolves(
        "test_deploy_k8s_declarations.py", "test_no_such_test_anywhere", source
    )


# --- the guard itself -----------------------------------------------------------------


def test_the_guard_finds_citations_to_check():
    """A pattern that silently stopped matching would pass the guard below vacuously."""
    hits = sum(
        len(cited_tests(line))
        for path in CORPUS
        for line in path.read_text(errors="replace").splitlines()
    )
    # 10 today. A floor just under it catches the pattern breaking without failing every
    # time a doc drops a citation.
    assert hits >= 8, (
        f"only {hits} test citations found across {len(CORPUS)} files -- the pattern has "
        "stopped matching, so the existence check below is passing on an empty set."
    )


def test_every_cited_test_exists():
    missing = []
    for path in CORPUS:
        if not path.is_file():
            continue
        for line_no, line in enumerate(
            path.read_text(errors="replace").splitlines(), 1
        ):
            for cited, name in cited_tests(line):
                if resolves(cited, name, path):
                    continue
                missing.append(
                    f"{path.relative_to(REPO)}:{line_no} cites {cited}::{name}"
                )

    assert not missing, (
        "a doc or module cites a test that no file defines, so the invariant it says is "
        "pinned may be pinned by nothing:\n  " + "\n  ".join(missing)
    )
