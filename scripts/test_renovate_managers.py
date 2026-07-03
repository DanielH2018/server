#!/usr/bin/env python3
"""Guard that every Renovate custom-regex manager still matches its live target(s).

renovate.json's five `customManagers` are hand-rolled regexes (the built-in ansible-galaxy /
pre-commit managers weren't reliably matching these paths — see the in-file descriptions). If a
template is renamed, a pin's formatting shifts, or a matchString is edited, a manager silently
matches ZERO files/lines and that dependency axis ages with no signal: the 8-day dependency-
dashboard-stale detector only catches Renovate dying *entirely*, not one manager regressing.

This compiles each manager's `managerFilePatterns` + `matchStrings` and asserts each finds >=1 file
AND >=1 in-file match across the tracked repo, so a regression fails CI at commit time instead of
surfacing as a silently-un-bumped dependency weeks later.

Run: uv run pytest scripts/test_renovate_managers.py
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_MANAGERS = json.loads((_REPO / "renovate.json").read_text())["customManagers"]


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=_REPO, text=True, capture_output=True, check=True
    ).stdout
    return out.splitlines()


def _to_python_regex(pattern: str) -> str:
    # Renovate/RE2 named groups are (?<name>...); Python's re wants (?P<name>...).
    return re.sub(r"\(\?<(\w+)>", r"(?P<\1>", pattern)


def _file_pattern_to_regex(fp: str) -> re.Pattern:
    # A Renovate managerFilePattern wrapped in /.../ is a regex matched against the repo-relative
    # path (all five here use that form). Strip the one leading + trailing slash.
    assert fp.startswith("/") and fp.endswith("/"), (
        f"expected a /regex/ file pattern: {fp}"
    )
    return re.compile(fp[1:-1])


@pytest.fixture(scope="module")
def tracked() -> list[str]:
    return _tracked_files()


@pytest.mark.parametrize(
    "mgr", _MANAGERS, ids=[m["description"].split(".")[0][:40] for m in _MANAGERS]
)
def test_custom_manager_matches_live_targets(mgr: dict, tracked: list[str]) -> None:
    assert mgr["customType"] == "regex"

    file_res = [_file_pattern_to_regex(fp) for fp in mgr["managerFilePatterns"]]
    matched_files = [f for f in tracked if any(r.search(f) for r in file_res)]
    assert matched_files, (
        f"Renovate manager {mgr['description'][:60]!r} matched NO tracked files — its "
        f"managerFilePatterns {mgr['managerFilePatterns']} no longer resolve to anything."
    )

    match_res = [re.compile(_to_python_regex(ms)) for ms in mgr["matchStrings"]]
    hits = sum(
        len(r.findall((_REPO / f).read_text()))
        for f in matched_files
        for r in match_res
    )
    assert hits > 0, (
        f"Renovate manager {mgr['description'][:60]!r} matched {len(matched_files)} file(s) but "
        f"its matchStrings found ZERO dependency lines — the regex has drifted from the file format."
    )


def _docker_manager() -> dict:
    for m in _MANAGERS:
        if m.get("datasourceTemplate") == "docker":
            return m
    raise AssertionError("no docker customManager in renovate.json")


# Digest-pinned (no tag) BY DESIGN — Renovate cannot version-track a bare digest, and the
# depName charclass excludes `@` precisely so these no longer false-pass as tracked (the
# pre-2026-07-02 charclass let `repo@sha256` slip through as a garbage depName Renovate
# silently ignored). Updates for these are the documented manual pull-digest-redeploy flow
# in the role's own compose comment. Anything ELSE digest-only is a mistake and still fails.
DIGEST_PINNED_EXEMPT = {
    # tdarr only ships dev-tagged builds (dev_X.Y.Z) with no stable tag line; stateful +
    # rewrites library files in place, so unvetted auto-updates are unacceptable.
    "ghcr.io/haveagitgat/tdarr",
}


def test_every_deployed_image_is_renovate_tracked() -> None:
    """Every `image:` line in an ACTIVE compose template must be captured by the docker manager.

    The aggregate test above only proves the manager matches SOMETHING — it passes even if one
    service's image slips the regex. A future image added untagged (implicit :latest), digest-only,
    or Jinja-templated in place of a literal tag would then age with no signal (the docker
    matchString requires an explicit :tag). This asserts per-image coverage so that gap fails CI
    at commit time. `latest`-tagged images ARE matched (then filtered by the packageRule), so they
    pass; a build-only service has no `image:` line and is skipped. archive/ is excluded by the
    single-level glob (mirrors the manager's own managerFilePatterns)."""
    match_res = [
        re.compile(_to_python_regex(ms)) for ms in _docker_manager()["matchStrings"]
    ]
    templates = sorted(
        (_REPO / "ansible/roles/containers").glob("*/templates/docker-compose.yml.j2")
    )
    assert templates, "no active compose templates found"
    untracked = []
    for t in templates:
        for line in t.read_text().splitlines():
            if not re.match(r"\s*image:\s*\S", line):
                continue
            digest_only = re.match(
                r"\s*image:\s*[\"']?(?P<repo>[^:\s\"'@]+)@sha256:", line
            )
            if digest_only and digest_only.group("repo") in DIGEST_PINNED_EXEMPT:
                continue
            if not any(r.search(line) for r in match_res):
                untracked.append(f"{t.relative_to(_REPO)}: {line.strip()}")
    assert not untracked, (
        "Deployed image line(s) NOT matched by the Renovate docker manager (untagged / digest-only "
        "/ templated) — they will age silently:\n" + "\n".join(untracked)
    )


def test_shellcheck_py_pins_in_lockstep() -> None:
    """prek.toml's shellcheck-py rev and pyproject.toml's `shellcheck-py==` pin must match.

    The two pins back DIFFERENT execution paths of the same tool — the prek hook env lints
    committed shell scripts, the pyproject dev dep lints RENDERED .sh.j2 output via
    validate_shell_templates — so a version skew means the two gates disagree about the same
    code. They are tracked by different Renovate datasources (github-tags vs pypi); a
    packageRule groups them into one PR, and this asserts that coupling actually held."""
    prek = (_REPO / "prek.toml").read_text()
    pyproject = (_REPO / "pyproject.toml").read_text()
    rev = re.search(
        r'repo = "https://github\.com/shellcheck-py/shellcheck-py"\s+rev = "v([^"]+)"',
        prek,
    )
    assert rev, "shellcheck-py repo/rev pin not found in prek.toml"
    pin = re.search(r'"shellcheck-py==([^"]+)"', pyproject)
    assert pin, "shellcheck-py== pin not found in pyproject.toml"
    assert rev.group(1) == pin.group(1), (
        f"shellcheck-py pins drifted: prek.toml rev v{rev.group(1)} vs "
        f"pyproject.toml =={pin.group(1)} — bump both together (they render/lint the same shell)."
    )
