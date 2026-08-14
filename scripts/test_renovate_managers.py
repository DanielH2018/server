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
_RENOVATE_CONFIG = json.loads((_REPO / "renovate.json").read_text())
_MANAGERS = _RENOVATE_CONFIG["customManagers"]
_PACKAGE_RULES = _RENOVATE_CONFIG["packageRules"]


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


def _slash_regex(pattern: str) -> re.Pattern:
    """A Renovate `matchCurrentValue` value, wrapped /like/this/ same as a managerFilePattern."""
    assert pattern.startswith("/") and pattern.endswith("/"), (
        f"expected a /regex/ matchCurrentValue: {pattern}"
    )
    return re.compile(pattern[1:-1])


def _minimatch_to_regex(glob: str) -> re.Pattern:
    """Renovate `matchFileNames` entries are minimatch globs, not regexes — `**` crosses `/`,
    a single `*` doesn't. Anchored full-match, repo-relative, forward-slash paths only."""
    out = []
    i = 0
    while i < len(glob):
        if glob[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _disabling_currentvalue_rules(package_rules: list[dict]) -> list[dict]:
    """packageRules that disable a dependency by matching its `currentValue` (e.g. the /^latest$/
    rule that started this whole guard — see test_every_k8s_role_image_is_renovate_tracked).

    Deliberately excludes package-NAME disables (e.g. influxdb's `matchPackageNames` rule) —
    those are a targeted, reviewed exemption for one dependency, not a value-shaped trap that can
    silently swallow a whole class of images the way the currentValue rule did."""
    return [
        r
        for r in package_rules
        if r.get("enabled") is False and "matchCurrentValue" in r
    ]


def _is_disabled_by_packagerule(
    current_value: str, rel_path: str, rules: list[dict]
) -> dict | None:
    """The rule that disables `current_value` for `rel_path`, or None if none do.

    A rule with no `matchFileNames` applies everywhere; one with `matchFileNames` applies only
    where at least one of its globs matches. This is the check the pre-2026-08-13 disable rule
    lacked scoping for: it matched every `currentValue: latest` regardless of file, silently
    disabling 13 k8s roles' worth of deliberately digest-pinned images."""
    for rule in rules:
        if not _slash_regex(rule["matchCurrentValue"]).search(current_value):
            continue
        file_globs = rule.get("matchFileNames")
        if file_globs is None or any(
            _minimatch_to_regex(g).match(rel_path) for g in file_globs
        ):
            return rule
    return None


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


def _manager_covering(path_fragment: str) -> dict:
    """Selected by the paths a manager watches, not by its datasource.

    There is more than one `docker`-datasource manager now — the compose templates and the k8s
    role defaults — so picking the first match on datasource alone would silently hand back the
    wrong one and make a coverage test assert against files it never looks at.
    """
    for m in _MANAGERS:
        if m.get("datasourceTemplate") != "docker":
            continue
        if any(path_fragment in p for p in m["managerFilePatterns"]):
            return m
    raise AssertionError(f"no docker customManager covering {path_fragment!r}")


def _docker_manager() -> dict:
    return _manager_covering("docker-compose")


def _k8s_image_manager() -> dict:
    return _manager_covering("roles/k8s")


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


# Images BUILT by this repo and pushed to the node-local registry, not pulled from an upstream
# registry — so there is no upstream ref for Renovate to compare against and no version for it to
# offer. Exempt by variable name rather than by pattern, so adding one is a deliberate act with a
# reviewer looking at it, the same rule the bridge allow-lists use.
#
# These are NOT untracked in practice: what actually moves is the FROM line in the Dockerfile each
# is built from, and Renovate's built-in dockerfile manager already watches those
# (test_every_dockerfile_is_renovate_visible asserts it sees them).
REGISTRY_BUILT_IMAGES = {
    "n8n_k8s_image",  # ansible/roles/containers/n8n/templates/Dockerfile.j2
    "n8n_k8s_runners_image",  # ansible/roles/containers/n8n/templates/Dockerfile-runners.j2
    "ical_proxy_k8s_image",  # ansible/roles/containers/ical-proxy/templates/Dockerfile.j2
    "code_server_k8s_image",  # ansible/roles/containers/code-server/templates/Dockerfile.j2
    "homelab_mcp_k8s_image",  # ansible/roles/k8s/homelab-mcp/templates/Dockerfile.j2
    "nut_k8s_image",  # ansible/roles/k8s/nut/templates/Dockerfile.j2
}


def test_every_k8s_role_image_is_renovate_tracked() -> None:
    """The sibling of the test above, for the cluster.

    A k8s role has no compose template to read: the `*_image:` vars in its defaults ARE the
    source of truth for what every pod runs. Nothing watched them until 2026-08-06 — 21 pins
    across 13 roles, the entire cluster fleet, ageing with no update signal — which surfaced
    only when pinning littlelink's `:latest` would otherwise have frozen it outright.

    Per-var rather than aggregate, for the same reason as the compose guard: the manager
    matching SOMETHING passes even when one role's image slips the regex. An untagged or
    digest-only pin does exactly that, because the matchString requires an explicit :tag.

    Regex-match alone is NOT sufficient, either (2026-08-13 review): the watchtower-era
    `/^latest$/` packageRule disabled the WHOLE dependency for any k8s image whose extracted
    currentValue was `latest` — including the deliberate `latest@sha256:...` digest pins
    (littlelink, tdarr, dri-device-plugin) — even though every one of those lines matched this
    manager's matchStrings just fine. Renovate never raised a single digest PR for 13 roles and
    this test stayed green throughout. So beyond matching, assert nothing DISABLES the match:
    walk packageRules the same way Renovate would and fail if an `enabled: false` rule applies
    to this file's currentValue.
    """
    match_res = [
        re.compile(_to_python_regex(ms)) for ms in _k8s_image_manager()["matchStrings"]
    ]
    disabling_rules = _disabling_currentvalue_rules(_PACKAGE_RULES)
    defaults = sorted((_REPO / "ansible/roles/k8s").glob("*/defaults/main.yml"))
    assert defaults, "no k8s role defaults found"
    untracked = []
    for f in defaults:
        rel_path = str(f.relative_to(_REPO))
        for line in f.read_text().splitlines():
            if not re.match(r"\s*\w*_image:\s*\S", line):
                continue
            if line.split(":", 1)[0].strip() in REGISTRY_BUILT_IMAGES:
                continue
            matches = (r.search(line) for r in match_res)
            m = next((match for match in matches if match), None)
            if m is None:
                untracked.append(f"{rel_path}: {line.strip()} — matched no manager")
                continue
            disabling_rule = _is_disabled_by_packagerule(
                m.group("currentValue"), rel_path, disabling_rules
            )
            if disabling_rule is not None:
                untracked.append(
                    f"{rel_path}: {line.strip()} — disabled by packageRule "
                    f"{disabling_rule['description'][:60]!r}"
                )
    assert not untracked, (
        "k8s role image pin(s) the Renovate k8s-defaults manager will NOT actually update "
        "(unmatched, or matched but disabled by a packageRule) — the pods run them and "
        "nothing will ever offer a bump:\n" + "\n".join(untracked)
    )


def test_disabling_currentvalue_rule_scoped_to_its_files() -> None:
    """Regression test for the exact bug the guard above now catches.

    Proves _is_disabled_by_packagerule actually fires — without this, the strengthened
    assertion above is vacuous the moment renovate.json is correct (it would pass whether or
    not the disabled-by-rule branch works at all, the same 'passes on regex-match alone'
    failure mode this whole file exists to prevent). Uses a fabricated rule, not the live
    config, so it stays true regardless of what renovate.json currently contains.
    """
    fake_rules = [
        {
            "matchCurrentValue": "/^latest$/",
            "matchFileNames": ["ansible/roles/containers/**"],
            "enabled": False,
        }
    ]
    # In scope: a compose template's `latest` is disabled.
    assert (
        _is_disabled_by_packagerule(
            "latest",
            "ansible/roles/containers/homepage/templates/docker-compose.yml.j2",
            fake_rules,
        )
        is not None
    )
    # Out of scope: the same currentValue in a k8s role default must NOT be caught by a rule
    # scoped to the compose plane — this is precisely what the un-scoped rule got wrong.
    assert (
        _is_disabled_by_packagerule(
            "latest", "ansible/roles/k8s/littlelink/defaults/main.yml", fake_rules
        )
        is None
    )
    # A currentValue the rule doesn't match at all is never disabled.
    assert (
        _is_disabled_by_packagerule(
            "v1.2.3",
            "ansible/roles/containers/homepage/templates/docker-compose.yml.j2",
            fake_rules,
        )
        is None
    )


# Every `image:` line in a k8s deployment template must come from a Jinja variable, not a
# literal — a literal bypasses the k8s-defaults customManager above entirely (it only scans
# defaults/main.yml, never templates/*.j2), so it would age with NO update signal at all, worse
# than even the `latest` disable bug. Empty by design: as of 2026-08-13 every k8s template's
# image is a var (the last 4 literals — homepage/peanut/home-assistant/zigbee2mqtt init
# containers — were hoisted to defaults/main.yml the same review cycle this test was added).
# Add an entry here only as a deliberate, reviewed exception; it defeats the point otherwise.
IMAGE_LITERAL_ALLOWLIST: frozenset[str] = frozenset()


def test_no_literal_image_lines_in_k8s_templates() -> None:
    """No k8s deployment template hard-codes an `image:` ref outside a Jinja variable."""
    literal_re = re.compile(r'^\s*image:\s*(?!["\']?\{\{)(?P<ref>\S.*)$')
    templates = sorted((_REPO / "ansible/roles/k8s").glob("*/templates/*.j2"))
    assert templates, "no k8s templates found"
    offenders = []
    for f in templates:
        for lineno, line in enumerate(f.read_text().splitlines(), start=1):
            if line.strip().startswith("#"):
                continue
            m = literal_re.match(line)
            if not m:
                continue
            if m.group("ref").strip() in IMAGE_LITERAL_ALLOWLIST:
                continue
            offenders.append(f"{f.relative_to(_REPO)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Literal `image:` line(s) in a k8s template — not a Jinja var, so the k8s-defaults "
        "customManager (which only scans defaults/main.yml) never sees it and it ages with "
        "zero update signal:\n" + "\n".join(offenders)
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


def test_python_version_pins_in_lockstep() -> None:
    """ci.yml, image-smoke.yml, and .python-version must pin the same Python minor version.

    A single Renovate customManager scans every workflow file (renovate.json), so one bump PR is
    meant to edit both `python-version:` pins together; nothing else asserts the coupling actually
    held. A skew would run the scripts suite under one interpreter in CI and boot-smoke changed
    images under another — a silent test/runtime mismatch. `.python-version` (the interpreter host
    `uv run` selects) is tracked by a SEPARATE Renovate manager (built-in pyenv) whose PR does NOT
    automerge, so it can lag the workflow bump — leaving the host on 3.N while CI moves to 3.N+1.
    Compared on major.minor (pyenv may carry a patch the workflow pin omits). Mirrors the
    shellcheck-py / portainer lockstep tests above.
    """
    ci = (_REPO / ".github/workflows/ci.yml").read_text()
    smoke = (_REPO / ".github/workflows/image-smoke.yml").read_text()
    dotver = (_REPO / ".python-version").read_text().strip()
    c = re.search(r'python-version:\s*"([^"]+)"', ci)
    s = re.search(r'python-version:\s*"([^"]+)"', smoke)
    assert c, "python-version pin not found in ci.yml"
    assert s, "python-version pin not found in image-smoke.yml"
    assert c.group(1) == s.group(1), (
        f"python-version pins drifted: ci.yml {c.group(1)} vs image-smoke.yml {s.group(1)} — bump "
        f"both together (they must run the scripts suite and image smoke on the same interpreter)."
    )

    def _minor(v: str) -> str:
        return ".".join(v.split(".")[:2])

    assert _minor(dotver) == _minor(c.group(1)), (
        f"python-version drifted: .python-version {dotver} vs the workflows' {c.group(1)} — bump "
        f"the pyenv .python-version to match (its Renovate PR doesn't automerge, so it can lag)."
    )
