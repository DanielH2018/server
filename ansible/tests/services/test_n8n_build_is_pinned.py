"""Guard: the npm package the n8n image installs is pinned, and Renovate can read the pin (#2213).

`roles/k8s/n8n-images/templates/Dockerfile.j2` ran `npm install -g fuzzball` with no version
until 2026-09-21, so every rebuild took whatever npm served that day and nothing recorded which
version a pod carried — the class #2149 closed for code-server. Two halves here: the Dockerfile
carries an exact version, and the renovate.json regex manager over that file extracts it, so a
bump arrives as a PR rather than as a silent change on the next rebuild.

Run: uv run pytest ansible/tests/services/test_n8n_build_is_pinned.py
"""

import json
import re

from _helpers import K8S_ROLES, REPO

DOCKERFILE = K8S_ROLES / "n8n-images" / "templates" / "Dockerfile.j2"
RENOVATE = REPO / "renovate.json"

_NPM_INSTALL = re.compile(r"^\s*RUN\s+npm\s+install\s+-g\s+(.+?)\s*$", re.MULTILINE)
_PINNED = re.compile(r"^[@A-Za-z0-9._/-]+@\d+\.\d+\.\d+$")


def unpinned_npm_installs(dockerfile: str) -> list[str]:
    """Every `npm install -g` package that carries no exact `@X.Y.Z` version.

    Pure over the text so the red half below can hand it a floating line without editing the
    tree. A `--flag` in the package list is skipped rather than reported as a package.
    """
    problems: list[str] = []
    for m in _NPM_INSTALL.finditer(dockerfile):
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
    assert re.search(r"npm install -g fuzzball@", DOCKERFILE.read_text()), (
        "the n8n Dockerfile no longer installs fuzzball — update this guard with the package"
    )


def test_every_npm_package_is_pinned_exactly() -> None:
    assert not unpinned_npm_installs(DOCKERFILE.read_text())


def test_renovate_reads_the_pin_from_the_dockerfile() -> None:
    manager = _fuzzball_manager()
    assert manager["datasourceTemplate"] == "npm"
    pattern = manager["managerFilePatterns"][0].strip("/")
    assert re.search(pattern, DOCKERFILE.relative_to(REPO).as_posix()), (
        "the manager's file pattern does not name the n8n Dockerfile"
    )
    (match_string,) = manager["matchStrings"]
    # Renovate's JS named group `(?<v>` is Python's `(?P<v>`; the same rewrite
    # scripts/tests/test_renovate_managers.py applies to every manager.
    m = re.search(match_string.replace("(?<", "(?P<"), DOCKERFILE.read_text())
    assert m, "the manager's matchString finds no pin in the Dockerfile"
    assert re.fullmatch(r"\d+\.\d+\.\d+", m.group("currentValue"))


def test_a_pinned_install_is_clean() -> None:
    assert unpinned_npm_installs("RUN npm install -g fuzzball@2.2.6\n") == []


def test_a_bare_install_is_flagged() -> None:
    assert unpinned_npm_installs("RUN npm install -g fuzzball\n") == ["fuzzball"]


def test_a_range_pin_is_flagged() -> None:
    assert unpinned_npm_installs("RUN npm install -g fuzzball@^2.2.0\n") == [
        "fuzzball@^2.2.0"
    ]
