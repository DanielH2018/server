"""Guard: the npm package both n8n images install is pinned, and Renovate can read each pin.

An unpinned `npm install -g` in `roles/k8s/n8n/templates/Dockerfile.j2`, or an unpinned
`pnpm add` in `Dockerfile-runners.j2`, takes whatever npm serves on each rebuild, and nothing
records which version a pod carries. Two halves here: each Dockerfile carries an exact version,
and the renovate.json regex manager over both files extracts it, so a bump arrives as a PR
rather than as a silent change on the next rebuild. The first half
reads the RENDERED build text; the second stays on the source, because that is the file Renovate
itself reads.

Run: uv run pytest ansible/tests/services/test_n8n_build_is_pinned.py
"""

import json
import re

import pytest

from _helpers import K8S_ROLES, REPO
from _k8s_render import rendered_build_text

# The source template, for the Renovate half alone: Renovate reads the file on disk, braces
# intact, so asserting its matchString against a render would claim something false about the
# real system. The pin halves read the rendered build text (#3175).
DOCKERFILES = (
    K8S_ROLES / "n8n" / "templates" / "Dockerfile.j2",
    K8S_ROLES / "n8n" / "templates" / "Dockerfile-runners.j2",
)
RENOVATE = REPO / "renovate.json"

_NPM_INSTALL = re.compile(r"^\s*RUN\s+npm\s+install\s+-g\s+(.+?)\s*$", re.MULTILINE)
# A comment line is skipped: the Dockerfile's prose names the command it explains.
_PNPM_ADD = re.compile(
    r"^(?!\s*#)[^\n]*?\bpnpm\s+add\s+(.+?)\s*(?:&&|\\|;|$)", re.MULTILINE
)
_PINNED = re.compile(r"^[@A-Za-z0-9._/-]+@\d+\.\d+\.\d+$")


def unpinned_npm_installs(dockerfile: str) -> list[str]:
    """Every `npm install -g` or `pnpm add` package that carries no exact `@X.Y.Z` version.

    Pure over the text so the red half below can hand it a floating line without editing the
    tree. A `--flag` in the package list is skipped rather than reported as a package.
    """
    problems: list[str] = []
    for m in [*_NPM_INSTALL.finditer(dockerfile), *_PNPM_ADD.finditer(dockerfile)]:
        for pkg in m.group(1).split():
            if pkg.startswith("-"):
                continue
            if not _PINNED.match(pkg):
                problems.append(pkg)
    return problems


def _fuzzball_manager() -> dict:
    managers = json.loads(RENOVATE.read_text())["customManagers"]
    matches = [m for m in managers if m.get("depNameTemplate") == "fuzzball"]
    assert len(matches) == 1, (
        "renovate.json needs exactly one regex manager for fuzzball"
    )
    return matches[0]


def test_the_dockerfile_installs_fuzzball() -> None:
    """Non-vacuity: the guard below passes over nothing if the install line is renamed away."""
    assert re.search(r"npm install -g fuzzball@", rendered_build_text("n8n")), (
        "the n8n Dockerfile no longer installs fuzzball — update this guard with the package"
    )


def test_the_runners_dockerfile_adds_fuzzball() -> None:
    """Non-vacuity for the runners image: its add line sits inside a multi-line RUN."""
    assert re.search(
        r"pnpm add fuzzball@", rendered_build_text("n8n", "Dockerfile-runners.j2")
    ), (
        "the runners Dockerfile no longer adds fuzzball — update this guard with the package"
    )


@pytest.mark.parametrize("template", ["Dockerfile.j2", "Dockerfile-runners.j2"])
def test_every_npm_package_is_pinned_exactly(template: str) -> None:
    assert not unpinned_npm_installs(rendered_build_text("n8n", template))


def test_renovate_reads_the_pin_from_the_dockerfile() -> None:
    manager = _fuzzball_manager()
    assert manager["datasourceTemplate"] == "npm"
    pattern = manager["managerFilePatterns"][0].strip("/")
    (match_string,) = manager["matchStrings"]
    # Renovate's JS named group `(?<v>` is Python's `(?P<v>`; the same rewrite
    # scripts/tests/test_renovate_managers.py applies to every manager. A non-capturing
    # `(?:` passes through unchanged.
    match_re = re.compile(match_string.replace("(?<", "(?P<"))
    for dockerfile in DOCKERFILES:
        assert re.search(pattern, dockerfile.relative_to(REPO).as_posix()), (
            f"the manager's file pattern does not name {dockerfile.name}"
        )
        m = match_re.search(dockerfile.read_text())
        assert m, f"the manager's matchString finds no pin in {dockerfile.name}"
        assert re.fullmatch(r"\d+\.\d+\.\d+", m.group("currentValue"))


def test_a_pinned_install_is_clean() -> None:
    assert unpinned_npm_installs("RUN npm install -g fuzzball@2.2.6\n") == []


def test_a_bare_install_is_flagged() -> None:
    assert unpinned_npm_installs("RUN npm install -g fuzzball\n") == ["fuzzball"]


def test_a_bare_pnpm_add_is_flagged() -> None:
    assert unpinned_npm_installs(
        "RUN cd /opt/runners/extra-js \\\n    && /usr/local/bin/pnpm add fuzzball \\\n"
        "    && ln -s a b\n"
    ) == ["fuzzball"]


def test_a_commented_pnpm_add_is_ignored() -> None:
    assert (
        unpinned_npm_installs("# a bare pnpm add fuzzball once pruned @n8n/di\n") == []
    )


def test_a_pinned_pnpm_add_is_clean() -> None:
    assert (
        unpinned_npm_installs("    && /usr/local/bin/pnpm add fuzzball@2.2.6 \\\n")
        == []
    )


def test_a_range_pin_is_flagged() -> None:
    assert unpinned_npm_installs("RUN npm install -g fuzzball@^2.2.0\n") == [
        "fuzzball@^2.2.0"
    ]
