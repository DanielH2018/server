#!/usr/bin/env python3
"""Guard that every Renovate custom-regex manager still matches its live target(s).

renovate.json's `customManagers` are hand-rolled regexes (the built-in ansible-galaxy /
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
    # path (all of them use this /regex/ form). Strip the one leading + trailing slash.
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
    # janitorr's only stable channel is the floating non-semver alias `jvm-stable`, which
    # Renovate can't order — and janitorr deletes real media, so updates must be deliberate:
    # manual pull-digest-redeploy (see the compose comment / role CLAUDE.md).
    "ghcr.io/schaka/janitorr",
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


# Renovate's BUILT-IN dockerfile manager's default managerFilePatterns, copied verbatim from source
# (lib/modules/manager/dockerfile/index.ts, verified against upstream 2026-07-13). The fleet's
# Dockerfile base pins are tracked ONLY by that manager (no custom manager covers them), so a build
# file renamed/added outside these shapes drops out of update tracking with no signal. NB the 2nd
# pattern's `[^/]*$` matches suffixed names too (`Dockerfile-runners.j2` IS visible), so this guard
# reflects exactly what Renovate scans — the earlier `[Cc]ontain` was a typo (matched a nonexistent
# `Containfile`, missed a real `Containerfile`); upstream is `[Cc]ontainer`.
DOCKERFILE_MANAGER_FILE_RES = [
    re.compile(r"(^|/|\.)([Dd]ocker|[Cc]ontainer)file$"),
    re.compile(r"(^|/)([Dd]ocker|[Cc]ontainer)file[^/]*$"),
]


def test_every_dockerfile_is_renovate_visible(tracked: list[str]) -> None:
    """Every FROM-bearing build file must sit where Renovate's dockerfile manager looks.

    The compose-template guard above covers `image:` lines; this is its sibling for built
    images. Discovery is by CONTENT (any tracked ansible/ file with a FROM line), not by
    name, so the check doesn't share the blind spot it guards against. Untagged / :latest
    FROMs are the deliberate rolling tier (build-on-recreate semantics — metabase, n8n,
    code-server) and need no version tracking; the version-bearing ones are exactly what
    the dockerfile manager must keep seeing."""
    from_re = re.compile(r"^FROM\s+\S+", re.MULTILINE)
    build_files = [
        f
        for f in tracked
        if f.startswith("ansible/")
        and not f.endswith(".md")
        and from_re.search((_REPO / f).read_text(errors="ignore"))
    ]
    assert build_files, (
        "no FROM-bearing build files found under ansible/ (discovery drifted?)"
    )
    escaped = [
        f
        for f in build_files
        if not any(r.search(f) for r in DOCKERFILE_MANAGER_FILE_RES)
    ]
    assert not escaped, (
        "Build file(s) with a FROM line that Renovate's dockerfile manager will NOT scan "
        "(name doesn't match its filePatterns) — their base-image pins will age silently:\n"
        + "\n".join(escaped)
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


def test_portainer_server_agent_pins_in_lockstep() -> None:
    """portainer-ce (server) and portainer/agent (Pi) image tags must match.

    The Portainer Agent API version tracks the Portainer server it connects to, so the two images
    MUST run the same version (portainer-agent/CLAUDE.md). They live in separate compose templates
    on separate hosts (daniel-server GitOps-auto-deployed vs daniel-pi manual-only), so a Renovate
    packageRule groups them into one no-automerge PR; this asserts the coupling actually held, so a
    skew (which silently breaks the Pi's Portainer environment with no alert) fails CI at commit time.
    """
    server = (
        _REPO / "ansible/roles/containers/portainer/templates/docker-compose.yml.j2"
    ).read_text()
    agent = (
        _REPO
        / "ansible/roles/containers/portainer-agent/templates/docker-compose.yml.j2"
    ).read_text()
    s = re.search(r"image:\s*portainer/portainer-ce:(\S+)", server)
    a = re.search(r"image:\s*portainer/agent:(\S+)", agent)
    assert s, "portainer/portainer-ce image pin not found in the portainer role"
    assert a, "portainer/agent image pin not found in the portainer-agent role"
    assert s.group(1) == a.group(1), (
        f"portainer pins drifted: portainer-ce {s.group(1)} vs agent {a.group(1)} — bump both "
        f"together (the agent API version must match the server it connects to)."
    )
