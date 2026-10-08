#!/usr/bin/env python3
"""Guard that every Renovate custom-regex manager still matches its live target(s).

renovate.json's `customManagers` are hand-rolled regexes (the built-in ansible-galaxy /
pre-commit managers weren't reliably matching these paths — see the in-file descriptions). If a
template is renamed, a pin's formatting shifts, or a matchString is edited, a manager silently
matches ZERO files/lines and that dependency axis ages with no signal: the 8-day dependency-
dashboard-stale detector only catches Renovate dying *entirely*, not one manager regressing.

This compiles each manager's `managerFilePatterns` + `matchStrings` and asserts each finds >=1 file
AND >=1 in-file match across the tracked repo, so a regression fails CI at commit time instead of
surfacing as a silently-un-bumped dependency weeks later. The regex translations and the
parsed config are in `_renovate.py`, and the `tracked` file list is a conftest fixture.
The Dockerfile and lockstep guards are in `test_renovate_dockerfiles.py`; the
release-download URL guard is in `test_renovate_release_urls.py`.

Run: uv run pytest scripts/tests/test_renovate_managers.py
"""

import re
from pathlib import PurePosixPath

import pytest

from _renovate import (
    _MANAGERS,
    _PACKAGE_RULES,
    _RENOVATE_CONFIG,
    _REPO,
    _disabling_currentvalue_rules,
    _file_pattern_to_regex,
    _is_disabled_by_packagerule,
    _k8s_image_manager,
    _resolve_group_name,
    _to_python_regex,
)


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


def test_no_custom_manager_tracks_the_retired_compose_plane() -> None:
    """No customManager may target ansible/roles/containers/**.

    Both cluster hosts are drained (daniel-server's containers_list is empty and Docker is
    uninstalled; daniel-box is all platform: k8s), so the only composes left are daniel-pi's
    five — and the Pi has has_gitops: false, no CI deploy path, and is deliberately untracked
    per operator decision. Every remaining compose is therefore either dead or hand-deployed,
    and tracking them only manufactures drift: a bump of a compose template for a service
    that runs in the cluster targets a pin nothing reads.

    Asserted rather than merely deleted, because re-adding a compose manager looks entirely
    reasonable in isolation — this is the context that makes it wrong.
    """
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
    silently re-enables scanning of node_modules, vendor, and test fixtures unless the
    inherited eight are restated alongside it.

    renovate-config-validator does not catch this — the shortened array is perfectly valid
    config, just wider in scope than the author intended.
    """
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


def test_group_vars_images_are_tracked() -> None:
    """Some customManager must scan inventory/group_vars/all.yml.

    crowdsec_k8s_image lives in group_vars, which is outside the k8s-defaults manager's
    file patterns AND outside the glob the per-image coverage test below uses — so without
    this check the WAF core, the one image whose staleness is a security problem, would be
    the single pin that neither could see.
    """
    scanned = [p for m in _MANAGERS for p in m["managerFilePatterns"]]
    assert any("group_vars" in p for p in scanned), (
        "no customManager scans inventory/group_vars/all.yml; an image pinned there "
        "(crowdsec_k8s_image) ages with no update signal"
    )


_CONTROL_PLANE_DEFAULTS = "ansible/roles/setup/k3s/defaults/main.yml"


def test_control_plane_version_pins_are_tracked() -> None:
    """Every `*_version:` pin in roles/setup/k3s/defaults must be matched by a customManager.

    k3s, MetalLB and Longhorn are pinned here, and the k8s-images manager one directory over
    scans roles/k8s/*/defaults and matches `_image:` keys, so neither its file patterns nor the
    per-image coverage test above can see these. Widening that test's glob would NOT have caught
    it — these are version vars, not image vars — which is why this is a separate guard.

    Written to read the pins out of the file rather than assert three known names, so a fourth
    component pinned here later (cilium, cert-manager, a k3s addon) fails this test until it is
    tracked, instead of quietly repeating the same escape.
    """
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


def test_coredns_extract_skips_the_upstream_stray_tags() -> None:
    """The coredns manager's extractVersionTemplate must read `v1.14.7` and skip `v011`.

    coredns/coredns carries stray tags `v002` ... `v011` beside its real `v1.x.y` releases. An
    extraction of `^v(?<version>.*)$` admits `v011`; Renovate then ranks `011` above `1.14.7` as
    a major and raises a PR whose download URL 404s and whose paired sha256 still belongs to
    1.14.7. The pin sits in the `k3s-bringup.yml` plane the GitOps deployer never applies, so
    nothing would have surfaced the break until someone ran that playbook by hand.

    Asserted on the pattern rather than on a version bound, because a bound of `^1\\.` would also
    hide a real 2.0.0 — the silent-staleness failure mode this manager exists to close.
    """
    coredns = [m for m in _MANAGERS if m.get("depNameTemplate") == "coredns/coredns"]
    assert len(coredns) == 1, (
        "expected exactly one coredns/coredns custom manager in renovate.json, found "
        f"{len(coredns)} — this test no longer guards what it names"
    )
    extract = re.compile(_to_python_regex(coredns[0]["extractVersionTemplate"]))

    tag = extract.fullmatch("v1.14.7")
    assert tag is not None and tag.group("version") == "1.14.7", (
        "the coredns extractVersionTemplate no longer reads a real release tag — the manager is "
        "inert and the host resolver ages with no signal"
    )
    stray = [t for t in ("v002", "v005", "v011") if extract.fullmatch(t)]
    assert not stray, (
        f"the coredns extractVersionTemplate admits upstream's stray tags {stray} — Renovate "
        "ranks those above 1.14.7 as a major and proposes a version with no release tarball (#3087)"
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
    "n8n_k8s_image",  # ansible/roles/k8s/n8n/templates/Dockerfile.j2
    "n8n_k8s_runners_image",  # ansible/roles/k8s/n8n/templates/Dockerfile-runners.j2
    "ical_proxy_k8s_image",  # ansible/roles/k8s/ical-proxy/templates/Dockerfile.j2
    "code_server_k8s_image",  # ansible/roles/k8s/code-server/templates/Dockerfile.j2
    "homelab_mcp_k8s_image",  # ansible/roles/k8s/homelab-mcp/templates/Dockerfile.j2
    # ansible/roles/k8s/karakeep/templates/Dockerfile.j2 — a chown layer over the pinned uv
    # base so the time-tagger runs as uid 1000. Its FROM is watched like the rest; the
    # pip pins the container installs at boot sit in deployment-time-tagger.yaml.j2 and have
    # their own regex manager.
    "karakeep_k8s_tagger_image",
    "nut_k8s_image",  # ansible/roles/k8s/nut/templates/Dockerfile.j2
    "pi_peer_backup_k8s_image",  # ansible/roles/k8s/pi-peer-backup/templates/Dockerfile.j2
    "terraria_k8s_image",  # ansible/roles/k8s/terraria/templates/Dockerfile.j2
    # ansible/roles/k8s/valheim/templates/Dockerfile.j2 — the BepInEx plugin DLLs. Its FROM is
    # watched like the rest; the MOD versions inside it are Thunderstore releases Renovate has
    # no manager for, and valheim/CLAUDE.md carries the by-hand bump procedure instead.
    "valheim_k8s_mods_image",
}


def test_every_k8s_role_image_is_renovate_tracked() -> None:
    """The sibling of the test above, for the cluster.

    A k8s role has no compose template to read: the `*_image:` vars in its defaults ARE the
    source of truth for what every pod runs. Without it the entire cluster fleet's pins (21
    across 13 roles) could age with no update signal.

    Per-var rather than aggregate, for the same reason as the compose guard: the manager
    matching SOMETHING passes even when one role's image slips the regex. An untagged or
    digest-only pin does exactly that, because the matchString requires an explicit :tag.

    Regex-match alone is NOT sufficient, either: a `/^latest$/` packageRule disables the WHOLE
    dependency for any k8s image whose extracted currentValue is `latest` — including the
    deliberate `latest@sha256:...` digest pins (littlelink, tdarr, dri-device-plugin) — even
    though every one of those lines matches this manager's matchStrings. So beyond matching,
    assert nothing DISABLES the match: walk packageRules the same way Renovate would and fail
    if an `enabled: false` rule applies to this file's currentValue.
    """
    match_res = [
        re.compile(_to_python_regex(ms)) for ms in _k8s_image_manager()["matchStrings"]
    ]
    disabling_rules = _disabling_currentvalue_rules(_PACKAGE_RULES)
    # group_vars/all.yml is included deliberately: crowdsec_k8s_image lives in group_vars, and
    # a role-defaults-only glob cannot see it — so the test written to catch an untracked
    # cluster image would be structurally blind to the one image (the WAF core) whose staleness
    # is a security problem.
    #
    # roles/setup/*/defaults/main.yml is included for the same reason: k3s_longhorn_restore_drill_image
    # (busybox:stable, roles/setup/k3s/defaults/main.yml) is one directory over from the k8s
    # role defaults, so a role-defaults-only glob would leave it matched by no manager and by no
    # test (this is the *_image: sibling of the *_version: pins in the same file that
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
    """Regression test for the exact bug the guard above catches.

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
# than even the `latest` disable bug. Empty by design: every k8s template's image is a var. Add
# an entry here only as a deliberate, reviewed exception; it defeats the point otherwise.
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


@pytest.mark.parametrize(
    ("dep_name", "rel_path", "datasource"),
    [
        ("prek", ".github/workflows/ci.yml", "pypi"),
        ("vale-cli/vale", ".github/workflows/ci.yml", "github-releases"),
        ("vale-cli/Google", ".vale.ini", "github-releases"),
    ],
)
def test_ci_toolchain_pins_have_their_own_group(
    dep_name: str, rel_path: str, datasource: str
) -> None:
    """A CI toolchain pin must resolve to its own group, never the container-images catch-all.

    The 'container images (non-major)' packageRule has no `matchFileNames`, so it matches
    every custom.regex dep by default — including the prek and Vale pins in ci.yml/.vale.ini,
    none of which is a container image. Renovate reuses a group's branch across whichever deps
    land in it, so a title generated for one dep can survive a force-push that swaps in
    another dep's diff. Each of these pins gets its own `{{depName}}` group instead, so its
    branch and title always name itself.
    """
    assert _resolve_group_name(dep_name, rel_path, "minor", datasource) == "{{depName}}"


def test_ci_toolchain_group_resolution_catches_the_catch_all() -> None:
    """Regression test for the exact bug the guard above catches.

    Without a file-scoped override, the broad non-major rule wins by default — proving
    `_resolve_group_name` isn't vacuously returning the right answer for every input, the same
    'passes on match alone' failure mode `test_disabling_currentvalue_rule_scoped_to_its_files`
    guards against for `enabled` rules.
    """
    fake_rules = [
        {
            "matchManagers": ["custom.regex"],
            "matchUpdateTypes": ["minor", "patch"],
            "groupName": "container images (non-major)",
        }
    ]
    assert (
        _resolve_group_name(
            "prek", ".github/workflows/ci.yml", "minor", "pypi", fake_rules
        )
        == "container images (non-major)"
    )


def _pin_digest_rule_for(path: str) -> dict | None:
    """The packageRule that turns pinDigests on for a docker pin in `path`, if any."""
    for rule in _PACKAGE_RULES:
        if (
            rule.get("pinDigests") is True
            and "custom.regex" in rule.get("matchManagers", [])
            and "docker" in rule.get("matchDatasources", [])
            and any(
                PurePosixPath(path).full_match(g)
                for g in rule.get("matchFileNames", [])
            )
        ):
            return rule
    return None


def test_every_k8s_image_file_gets_pin_digests_from_a_package_rule(
    tracked: list[str],
) -> None:
    """The top-level `pinDigests: true` does not reach a regex-managed pin (#3281).

    Renovate's regex custom manager ships `defaultConfig = { pinDigests: false }`, and a
    manager default is merged over the repository's top-level options, so only a packageRule
    turns digest pinning back on. Without one, a tag-only pin gets no pinDigest update and a
    version bump carries no newDigest, so the manager's template never writes a digest.

    The rule must also re-slug the group: Renovate's built-in `pinDigest` config groups every
    pin into one branch, which would restart every pinned service from a single PR.
    """
    patterns = [
        _file_pattern_to_regex(p) for p in _k8s_image_manager()["managerFilePatterns"]
    ]
    files = [f for f in tracked if any(p.search(f) for p in patterns)]
    assert "ansible/roles/k8s/loki-homelab/defaults/main.yml" in files, (
        "the k8s-images manager no longer scans a known role's defaults; this census is empty"
    )
    uncovered = [f for f in files if _pin_digest_rule_for(f) is None]
    assert not uncovered, (
        f"no packageRule sets pinDigests for the k8s-images manager's pins in {len(uncovered)} "
        f"files (first: {uncovered[:3]}); "
        "the regex manager's own default leaves them tag-only"
    )
    rule = _pin_digest_rule_for(files[0])
    assert rule is not None
    assert rule is _PACKAGE_RULES[-1], (
        "the pinDigests rule must be the LAST packageRule: placed earlier, a 2026-10-03 dry run "
        "put every k8s image update back on one `k8s-image-depname` branch"
    )
    assert rule["groupSlug"] == "{{{groupName}}}", (
        "the pinDigests rule must slug each pin by its own group name, or Renovate's built-in "
        "pinDigest config puts every service's pin on one branch"
    )
    assert rule["group"]["commitMessageTopic"] == "{{{groupName}}}", (
        "the pinDigests rule must keep the group name in the PR title, or a denied role's pin PR "
        "loses the k8s_autodeploy: false marker the renovate_agent reads"
    )
