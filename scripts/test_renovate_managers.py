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


def test_no_custom_manager_tracks_the_retired_compose_plane() -> None:
    """No customManager may target ansible/roles/containers/**.

    Replaces the old per-compose coverage test, whose premise expired with the k3s migration.
    Both cluster hosts are drained (daniel-server's containers_list is empty and Docker is
    uninstalled; daniel-box is all platform: k8s), so the only composes left are daniel-pi's
    five — and the Pi has has_gitops: false, no CI deploy path, and is deliberately untracked
    per operator decision. Every remaining compose is therefore either dead or hand-deployed,
    and tracking them only manufactured drift: PR #67 bumped the pihole and traefik compose
    templates for services that now run in the cluster, against pins nothing reads.

    Asserted rather than merely deleted, because re-adding a compose manager looks entirely
    reasonable in isolation — this is the context that makes it wrong."""
    offenders = []
    for m in _MANAGERS:
        for pattern in m["managerFilePatterns"]:
            if "roles/containers" in pattern:
                offenders.append(f"{pattern}: {m.get('description', '')[:80]}")
    assert not offenders, (
        "customManager(s) targeting the retired compose plane — those pins are dead or "
        "Pi-only (untracked by decision):\n" + "\n".join(offenders)
    )


def test_ignore_paths_keeps_the_inherited_preset_globs() -> None:
    """renovate.json's ignorePaths must still carry every glob from :ignoreModulesAndTests.

    A local ignorePaths REPLACES the preset's array rather than appending to it, and
    config:recommended pulls :ignoreModulesAndTests in. So adding one repo-specific entry
    (roles/containers/archive/**) silently re-enables scanning of node_modules, vendor, and
    test fixtures unless the inherited eight are restated alongside it.

    renovate-config-validator does not catch this — the shortened array is perfectly valid
    config, just wider in scope than the author intended."""
    inherited = {
        "**/node_modules/**",
        "**/bower_components/**",
        "**/vendor/**",
        "**/examples/**",
        "**/__tests__/**",
        "**/test/**",
        "**/tests/**",
        "**/__fixtures__/**",
    }
    configured = set(_RENOVATE_CONFIG.get("ignorePaths", []))
    assert inherited <= configured, (
        "ignorePaths dropped glob(s) inherited from :ignoreModulesAndTests via "
        "config:recommended — a local array replaces the preset's, so these must be "
        f"restated: {sorted(inherited - configured)}"
    )


def test_archived_compose_plane_is_ignored() -> None:
    """ignorePaths must cover roles/containers/archive/**.

    The retired Compose roles hold 62 image pins that deploy nothing. Every customManager is
    pinned to a live file (test_no_custom_manager_tracks_the_retired_compose_plane guards
    that), but the BUILT-IN docker-compose manager has no such scoping and scans them anyway
    — which is how PR #42 came to bump getmeili/meilisearch in archive/karakeep while the
    live pin sits in roles/k8s/karakeep/defaults/main.yml."""
    assert "ansible/roles/containers/archive/**" in _RENOVATE_CONFIG.get(
        "ignorePaths", []
    ), (
        "archived compose roles are being scanned by the built-in docker-compose manager; "
        "they deploy nothing, so every pin there is a dead-path PR"
    )


def test_group_vars_images_are_tracked() -> None:
    """Some customManager must scan inventory/group_vars/all.yml.

    crowdsec_k8s_image was hoisted out of its role defaults into group_vars, which put it
    outside the k8s-defaults manager's file patterns AND outside the glob the per-image
    coverage test below uses — so the WAF core, the one image whose staleness is a security
    problem, was the single pin that neither could see."""
    scanned = [p for m in _MANAGERS for p in m["managerFilePatterns"]]
    assert any("group_vars" in p for p in scanned), (
        "no customManager scans inventory/group_vars/all.yml; an image pinned there "
        "(crowdsec_k8s_image) ages with no update signal"
    )


_CONTROL_PLANE_DEFAULTS = "ansible/roles/setup/k3s/defaults/main.yml"


def test_control_plane_version_pins_are_tracked() -> None:
    """Every `*_version:` pin in roles/setup/k3s/defaults must be matched by a customManager.

    k3s, MetalLB and Longhorn were pinned here with no manager reaching the file at all: the
    k8s-images manager one directory over scans roles/k8s/*/defaults and matches `_image:` keys,
    so neither its file patterns nor the per-image coverage test above could ever see these
    (2026-08-15 review H5). Widening that test's glob would NOT have caught it — these are
    version vars, not image vars — which is why this is a separate guard.

    Written to read the pins out of the file rather than assert three known names, so a fourth
    component pinned here later (cilium, cert-manager, a k3s addon) fails this test until it is
    tracked, instead of quietly repeating the same escape."""
    text = (_REPO / _CONTROL_PLANE_DEFAULTS).read_text()
    pins = re.findall(r"^([a-z0-9_]*_version):\s*\"?(v[^\"\s]+)", text, re.MULTILINE)
    assert pins, (
        f"no `<name>_version: vX.Y.Z` pins found in {_CONTROL_PLANE_DEFAULTS} — either they "
        "moved or their formatting changed, so this test is no longer guarding anything"
    )
    covering = [
        m
        for m in _MANAGERS
        if any(
            _file_pattern_to_regex(p).search(_CONTROL_PLANE_DEFAULTS)
            for p in m["managerFilePatterns"]
        )
    ]
    untracked = [
        f"{name}: {value}"
        for name, value in pins
        if not any(
            re.search(_to_python_regex(ms), f"{name}: {value}")
            for m in covering
            for ms in m["matchStrings"]
        )
    ]
    assert not untracked, (
        "control-plane version pin(s) no customManager tracks — the Kubernetes version, the "
        "LoadBalancer implementation, or the storage layer under every PVC would age with no "
        "update signal:\n" + "\n".join(untracked)
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
    "n8n_k8s_image",  # ansible/roles/k8s/n8n-images/templates/Dockerfile.j2
    "n8n_k8s_runners_image",  # ansible/roles/k8s/n8n-images/templates/Dockerfile-runners.j2
    "ical_proxy_k8s_image",  # ansible/roles/k8s/ical-proxy/templates/Dockerfile.j2
    "code_server_k8s_image",  # ansible/roles/k8s/code-server/templates/Dockerfile.j2
    "homelab_mcp_k8s_image",  # ansible/roles/k8s/homelab-mcp/templates/Dockerfile.j2
    "nut_k8s_image",  # ansible/roles/k8s/nut/templates/Dockerfile.j2
    "pi_peer_backup_k8s_image",  # ansible/roles/k8s/pi-peer-backup/templates/Dockerfile.j2
    "terraria_k8s_image",  # ansible/roles/k8s/terraria/templates/Dockerfile.j2
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
    # group_vars/all.yml is included deliberately: crowdsec_k8s_image was hoisted out of its
    # role defaults into group_vars, and this glob's role-defaults-only form could not see it —
    # so the test written to catch an untracked cluster image was structurally blind to the one
    # image (the WAF core) whose staleness is a security problem.
    #
    # roles/setup/*/defaults/main.yml is included for the same reason: k3s_longhorn_restore_drill_image
    # (busybox:stable, roles/setup/k3s/defaults/main.yml) is one directory over from the k8s
    # role defaults this glob covered, matched by no manager and by no test, until 2026-08-27
    # (this is the *_image: sibling of the *_version: pins in the same file that
    # test_control_plane_version_pins_are_tracked already covers — separate guard, same file,
    # different key, so neither doubles up on the other).
    defaults = sorted((_REPO / "ansible/roles/k8s").glob("*/defaults/main.yml"))
    assert defaults, "no k8s role defaults found"
    defaults += sorted((_REPO / "ansible/roles/setup").glob("*/defaults/main.yml"))
    defaults.append(_REPO / "ansible/inventory/group_vars/all.yml")
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
            "ansible/roles/containers/dozzle/templates/docker-compose.yml.j2",
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
            "ansible/roles/containers/dozzle/templates/docker-compose.yml.j2",
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
    name, so the check doesn't share the blind spot it guards against.

    Whether the FROM carries a version is a separate question, and its own test below —
    this one is purely about file NAMING, so a build file renamed out of the manager's
    filePatterns still fails here even when its pin is explicit."""
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


# A FROM line's image reference, plus the stage name a multi-stage build gives it.
_FROM_RE = re.compile(
    r"^FROM\s+(?P<ref>\S+)(?:\s+[Aa][Ss]\s+(?P<stage>\S+))?", re.MULTILINE
)


def test_no_built_image_floats_on_an_unpinned_base(tracked: list[str]) -> None:
    """No FROM may float: every base needs an explicit version tag or a digest.

    This test used to be the exemption it now forbids. The sibling above blessed untagged
    and `:latest` FROMs as "the deliberate rolling tier (build-on-recreate semantics)" —
    which was true only under Docker Compose, where `build: always` plus the weekly
    roles/containers/common/tasks/redeploy_cron.yml redeploy forced the rebuild that picked
    up new base layers. The k3s migration archived that cron's last caller on 2026-08-14 and
    put nothing in its place, so from then on a floating FROM had NO updater whatsoever:
    Renovate has no version to bump, so no PR is raised, so no commit lands, so gitops never
    ticks and no rebuild ever runs. Three images (n8n, n8n-runners, code-server) drifted that
    way until 2026-08-19 — n8n stuck on 2.34.6 while upstream shipped 2.35.4.

    So the rule inverts: an explicit tag is what makes the base a tracked dependency with a
    PR, CI and a review, exactly like every pulled image in the fleet. A `:latest@sha256:...`
    digest pin is accepted — that is a real, Renovate-bumpable pin, and it is the shape the
    k8s roles already use for mutable-tag upstreams.

    Multi-stage internal references (`FROM builder`) are skipped: a stage name declared
    earlier in the same file is not an upstream image and has nothing to pin."""
    build_files = [
        f
        for f in tracked
        if f.startswith("ansible/")
        and not f.endswith(".md")
        and not f.startswith("ansible/roles/containers/archive/")
        and _FROM_RE.search((_REPO / f).read_text(errors="ignore"))
    ]
    assert build_files, (
        "no FROM-bearing build files found under ansible/ (discovery drifted?)"
    )
    floating = []
    for f in build_files:
        stages: set[str] = set()
        for m in _FROM_RE.finditer((_REPO / f).read_text(errors="ignore")):
            ref = m.group("ref")
            if m.group("stage"):
                stages.add(m.group("stage").lower())
            # An earlier stage in the same file, not an upstream image.
            if ref.lower() in stages:
                continue
            # A digest is a pin in its own right, whatever the tag says.
            if "@sha256:" in ref:
                continue
            # Strip the registry host before looking for a tag, so the `:5000` in a
            # `localhost:5000/foo` host:port is never mistaken for one.
            tag = ref.rsplit("/", 1)[-1].partition(":")[2]
            # A tag naming a LINE rather than a release still moves — `python:3.14-slim`
            # re-points at every 3.14.x, `debian:bookworm-slim` at every point release. Renovate
            # can only offer a TAG change for those (3.14 -> 3.15), never the movement inside
            # one, so the tag reads pinned while the base drifts. A release tag is the case
            # where the tag alone suffices, and two dots is what separates the two:
            # `2.35.4` and `4.133.0-ls358` are releases, `3.14-slim` / `3.22` / `bookworm-slim`
            # are lines. A line tag must therefore carry a digest as well.
            if not tag or tag == "latest" or tag.count(".") < 2:
                floating.append(f"{f}: FROM {ref}")
    assert not floating, (
        "Build file(s) whose base image can still move. Renovate cannot bump a tag with no "
        "version in it, nor the movement inside a line tag, so these have no update path — "
        "not a PR, not an alert, not a rebuild. Pin an exact release tag, or keep the line "
        "tag and add the @sha256 digest beside it:\n" + "\n".join(floating)
    )


def test_n8n_base_pins_in_lockstep() -> None:
    """n8n's app and task-runner base pins must name the same tag.

    The two images are version-coupled: the runners execute Code-node tasks on behalf of the
    n8n they serve, and the runners Dockerfile pins pnpm to the base's own store version. A
    skew does not fail the build — it surfaces at RUNTIME as every Code-node workflow timing
    out while both pods stay Ready, which is precisely the 2026-08-06 `@n8n/di` failure that
    file's header records. Renovate groups them into one PR (the "n8n (lockstep: app + task
    runners)" packageRule); this asserts the coupling actually held.

    Compares the TAG and stops at the `@`. Both pins are `stable@sha256:...`, and their
    digests necessarily DIFFER — they are two different images. What has to match is the
    channel each one follows, because that is what decides they describe the same release.
    The check reads the same way for a plain version pin, so it survives a move back to one.

    The same shape as test_shellcheck_py_pins_in_lockstep below, and for the same reason: a
    grouping rule expresses intent, only a test enforces it."""
    root = _REPO / "ansible/roles/k8s/n8n-images/templates"
    app = re.search(
        r"^FROM\s+n8nio/n8n:([^@\s]+)",
        (root / "Dockerfile.j2").read_text(),
        re.MULTILINE,
    )
    runners = re.search(
        r"^FROM\s+n8nio/runners:([^@\s]+)",
        (root / "Dockerfile-runners.j2").read_text(),
        re.MULTILINE,
    )
    assert app, "no `FROM n8nio/n8n:<tag>` in Dockerfile.j2"
    assert runners, "no `FROM n8nio/runners:<tag>` in Dockerfile-runners.j2"
    assert app.group(1) == runners.group(1), (
        f"n8n base pins have drifted apart: app follows {app.group(1)}, runners follows "
        f"{runners.group(1)}. They are version-coupled — a skew surfaces as Code-node "
        "workflows failing at runtime, not as a build error. Move both together."
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


_DOWNLOAD_URL_FILES = (".github/workflows/ci.yml", ".vale.ini")


def _covered_spans(path: str, text: str) -> list[tuple[int, int]]:
    """Every span in `text` that some customManager scanning `path` actually matches."""
    spans: list[tuple[int, int]] = []
    for mgr in _MANAGERS:
        if not any(
            _file_pattern_to_regex(p).search(path) for p in mgr["managerFilePatterns"]
        ):
            continue
        for ms in mgr["matchStrings"]:
            for m in re.finditer(_to_python_regex(ms), text):
                spans.append(m.span())
    return spans


def _uncovered_version_occurrences(path: str) -> list[str]:
    """Occurrences of a pinned release version that no matchString covers.

    The verdict both halves of the red-proof below share. A release-download URL carries its
    version more than once — `.../download/v3.18.0/vale_3.18.0_Linux_64-bit.tar.gz` — and a
    manager matching only the tag rewrites half of it on a bump, producing a 404. Asserting that
    the pin LINE is matched cannot see that; asserting that every occurrence of the captured
    version is matched can.
    """
    text = (_REPO / path).read_text()
    spans = _covered_spans(path, text)
    captured = {m.group(1) for m in re.finditer(r"releases/download/v([\d.]+)/", text)}
    uncovered = []
    for version in captured:
        for occ in re.finditer(re.escape(version), text):
            if not any(s <= occ.start() and occ.end() <= e for s, e in spans):
                line = text[: occ.start()].count("\n") + 1
                uncovered.append(f"{path}:{line}: {version}")
    return uncovered


def test_every_release_download_version_occurrence_is_tracked() -> None:
    """A pinned release URL must have EVERY occurrence of its version tracked, not just the tag.

    2026-08-31 review: the Vale binary was pinned with no manager at all, and the first proposed
    manager matched only the tag — which would have rewritten
    `.../download/v3.19.0/vale_3.18.0_Linux_64-bit.tar.gz`, a 404. `curl -sSL` carries no `-f`, so
    the step writes the error body and dies at `tar xzf` instead, and the bump rides in a shared
    automerge group where it stalls every other non-major update bundled with it.
    """
    uncovered = [
        p for f in _DOWNLOAD_URL_FILES for p in _uncovered_version_occurrences(f)
    ]
    assert not uncovered, (
        "a pinned release version occurs where no customManager matchString reaches, so a "
        "Renovate bump would rewrite only part of the URL and leave a broken download:\n"
        + "\n".join(uncovered)
    )


def test_a_tag_only_matchstring_is_flagged(tmp_path) -> None:
    """The rejecting half: the tag-only manager must come back uncovered, or the check is inert."""
    text = (
        "          curl -sSL -o /tmp/vale.tar.gz \\\n"
        "            https://github.com/vale-cli/vale/releases/download/v3.18.0/"
        "vale_3.18.0_Linux_64-bit.tar.gz\n"
    )
    tag_only = [
        (m.span())
        for m in re.finditer(
            r"vale-cli/vale/releases/download/v(?P<currentValue>[\d.]+)/", text
        )
    ]
    captured = {m.group(1) for m in re.finditer(r"releases/download/v([\d.]+)/", text)}
    uncovered = [
        occ.start()
        for version in captured
        for occ in re.finditer(re.escape(version), text)
        if not any(s <= occ.start() and occ.end() <= e for s, e in tag_only)
    ]
    assert uncovered, (
        "the check no longer sees the asset-name occurrence a tag-only matchString misses"
    )
