#!/usr/bin/env python3
"""Guards on renovate.json's config and packageRules that Renovate's own extract cannot see.

Whether each custom manager still matches its pins is CI's job: `renovate_config.sh extract`
runs Renovate's extract and `scripts/validate/renovate_extract.py` holds it against the
expected-dependency table, including every `_image:` key and control-plane version. What stays
here is what an extract does not report: the preset globs `ignorePaths` must restate, which
packageRule disables or groups a dep, and the `image:` literals no manager is pointed at.
The Dockerfile and lockstep guards are in `test_renovate_dockerfiles.py`.

Run: uv run pytest scripts/tests/test_renovate_managers.py
"""

import json
import re
from pathlib import PurePosixPath

import pytest

from lib.repo_paths import ALL_VARS, REPO
from validate.renovate_extract import IMAGE_DEFAULTS, REGISTRY_BUILT_IMAGES
from _renovate import (
    EXPECTED_DEPS,
    _PACKAGE_RULES,
    _RENOVATE_CONFIG,
    _REPO,
    _disabling_currentvalue_rules,
    _is_disabled_by_packagerule,
    _resolve_group_name,
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


def test_coredns_allowed_versions_skip_the_upstream_stray_tags() -> None:
    """coredns/coredns carries stray tags `v002` ... `v011` beside its releases.

    Renovate would rank `v011` above 1.14.7 and raise a PR whose download URL 404s and whose paired
    digest stays at 1.14.7. Asserted on the pattern rather than on a version bound, because a
    bound would hide a real 2.0.0.
    """
    (rule,) = [
        r
        for r in json.loads((REPO / "renovate.json").read_text())["packageRules"]
        if r.get("matchPackageNames") == ["coredns/coredns"] and "allowedVersions" in r
    ]
    allowed = re.compile(rule["allowedVersions"].strip("/"))
    assert allowed.fullmatch("v1.14.7")
    assert allowed.fullmatch("v2.0.0")
    assert not [t for t in ("v002", "v005", "v011") if allowed.fullmatch(t)]


def _image_tag(ref: str) -> str:
    """The tag of an image reference, after any registry host and before any digest."""
    return ref.partition("@")[0].rsplit("/", 1)[-1].partition(":")[2]


def test_no_k8s_role_image_is_disabled_by_a_package_rule() -> None:
    """A `/^latest$/` packageRule once disabled every k8s image whose tag is `latest`.

    That swallowed the deliberate `latest@sha256:...` digest pins (littlelink, tdarr,
    dri-device-plugin) even though the manager extracts every one of them. So beyond the
    extract, walk the disabling packageRules the way Renovate would and fail if one applies
    to the tag a pin carries.
    """
    # fact: k8s/roles/texbrain/CLAUDE.md#The image comes from a fork, and why
    disabling_rules = _disabling_currentvalue_rules(_PACKAGE_RULES)
    defaults = sorted((_REPO / "k8s/roles").glob("*/defaults/main.yml"))
    defaults += [_REPO / p for p in IMAGE_DEFAULTS if "*" not in p]
    assert ALL_VARS in defaults, "the census lost inventory/group_vars/all.yml"
    disabled = []
    for f in defaults:
        rel_path = str(f.relative_to(_REPO))
        for line in f.read_text().splitlines():
            m = re.match(r"\s*(\w*_image):\s*[\"']?([^\s\"'#]+)", line)
            if not m or m.group(1) in REGISTRY_BUILT_IMAGES:
                continue
            rule = _is_disabled_by_packagerule(
                _image_tag(m.group(2)), rel_path, disabling_rules
            )
            if rule is not None:
                disabled.append(
                    f"{rel_path}: {line.strip()} — disabled by packageRule "
                    f"{rule['description'][:60]!r}"
                )
    assert not disabled, (
        "k8s role image pin(s) a packageRule disables — the pods run them and nothing will "
        "ever offer a bump:\n" + "\n".join(disabled)
    )


@pytest.mark.parametrize(
    ("ref", "tag"),
    [
        ("ghcr.io/a/b:latest@sha256:" + "0" * 64, "latest"),
        ("localhost:5000/n8n:1.2", "1.2"),
        ("a/b@sha256:" + "0" * 64, ""),
    ],
)
def test_image_tag_reads_past_the_registry_port_and_the_digest(
    ref: str, tag: str
) -> None:
    assert _image_tag(ref) == tag


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
            "latest", "k8s/roles/littlelink/defaults/main.yml", fake_rules
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


# Every `image:` line in a k8s deployment template must come from a Jinja variable: a literal
# bypasses the k8s-defaults customManager (it only scans defaults/main.yml, never templates/*.j2)
# and ages with no update signal. Add an entry only as a deliberate, reviewed exception.
# `rancher/k3s-upgrade` is the one exception: a SUC Plan's `upgrade.image` names the repository
# alone, and SUC appends the Plan's `version` as the tag, so the pin that ages is `k3s_version`,
# which an annotation tracks (ADR-0025).
IMAGE_LITERAL_ALLOWLIST: frozenset[str] = frozenset({"rancher/k3s-upgrade"})


def test_no_literal_image_lines_in_k8s_templates() -> None:
    """No k8s deployment template hard-codes an `image:` ref outside a Jinja variable."""
    literal_re = re.compile(r'^\s*image:\s*(?!["\']?\{\{)(?P<ref>\S.*)$')
    templates = sorted((_REPO / "k8s/roles").glob("*/templates/*.j2"))
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
        ("prek", ".github/actions/setup/action.yml", "pypi"),
        # python is absent on purpose: its minor and major bumps resolve to the cross-manager
        # `python (manual` group, held by test_renovate_python_group.py.
        ("node", ".github/actions/setup/action.yml", "node-version"),
        ("vale-cli/vale", ".github/workflows/ci.yml", "github-releases"),
        ("goreleaser/nfpm", ".github/workflows/ci.yml", "github-release-attachments"),
        ("vale-cli/Google", ".vale.ini", "github-releases"),
    ],
)
def test_ci_toolchain_pins_have_their_own_group(
    dep_name: str, rel_path: str, datasource: str
) -> None:
    """A CI toolchain pin must resolve to its own group, never the container-images catch-all.

    The 'container images (non-major)' packageRule has no `matchFileNames`, so it matches
    every custom.regex dep by default — including the prek, Python and Node pins in the CI
    setup action and the Vale pins in ci.yml/.vale.ini, none of which is a container image. Renovate reuses a group's branch across whichever deps
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


def test_every_k8s_image_file_gets_pin_digests_from_a_package_rule() -> None:
    """The top-level `pinDigests: true` does not reach a regex-managed pin.

    Renovate's regex custom manager ships `defaultConfig = { pinDigests: false }`, and a
    manager default is merged over the repository's top-level options, so only a packageRule
    turns digest pinning back on. Without one, a tag-only pin gets no pinDigest update and a
    version bump carries no newDigest, so the manager's template never writes a digest.

    The rule must also re-slug the group: Renovate's built-in `pinDigest` config groups every
    pin into one branch, which would restart every pinned service from a single PR.
    """
    files = sorted(
        {
            d.file
            for d in EXPECTED_DEPS
            if d.manager == "regex"
            and d.datasource == "docker"
            and d.file.startswith("k8s/roles/")
            and d.file.endswith("/defaults/main.yml")
        }
    )
    assert "k8s/roles/loki-homelab/defaults/main.yml" in files, (
        "no docker dep extracted from a known role's defaults; this census is empty"
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


def test_a_pin_digest_automerges_unless_the_role_is_denied_auto_deploy():
    """Renovate types the update the k8s-images template makes possible as `pinDigest`.

    A rule matching only `digest` automerged none of the tag-only pins, so each opened a
    hand-merge PR. The denylist rule must stay unscoped by update type, or a denied
    role's pinDigest would fall through to the automerge rule above it.
    """
    assert any(
        r.get("automerge") and "pinDigest" in r.get("matchUpdateTypes", [])
        for r in _PACKAGE_RULES
    ), "no automerge rule matches updateType pinDigest"
    denylist = [
        r
        for r in _PACKAGE_RULES
        if str(r.get("groupName", "")).startswith("k8s images (manual")
    ]
    assert len(denylist) == 1, denylist
    assert "matchUpdateTypes" not in denylist[0], denylist[0]["matchUpdateTypes"]
