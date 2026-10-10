#!/usr/bin/env python3
"""Every external image pin must carry an `@sha256:` digest, not a tag alone.

Two rules live here. The base-image rule below is the older and stricter one: it has no
exemptions. The fleet rule (`test_every_external_image_pin_carries_a_digest`, issue #3281)
extends the same requirement to every `*_image` default, so the repo has one pinning policy
rather than a digest for half the fleet and a bare tag for the rest. Renovate adds the
digests itself (`pinDigests`, re-enabled for the regex manager by PR #3326), so a bare pin
outside `PENDING_DIGEST_PINS` is a pin Renovate could not see or was told to skip.

WHY THE BASE-IMAGE RULE HAS NO EXEMPTIONS. A tag is mutable: the publisher can re-push it with new bytes and every
consumer silently changes on its next pull. For an application image that barely matters here,
because the tags this repo pins are exact upstream releases (`traefik:v3.7.11`,
`lscr.io/linuxserver/sonarr:4.0.17.2952-ls312`) and a publisher re-pushing one of those is
rare and newsworthy. The base images are the opposite case on both counts. `alpine:3.24` and
`python:3.14-alpine` name a patch *stream*, so upstream re-pushes them as a matter of routine,
and they are the init containers, probes and sidecars nobody watches — 20 of the 56 tag-only
references on the k8s plane.

WHAT THAT COSTS, concretely. A tag-only reference means the bytes a run validates are not the
bytes a later run gets. That weakens every read that assumes a name identifies bytes: a
`--dry-run`, a digest in a rollback, an image the deployer thinks it already pinned. Leaving
the most-frequently-re-pushed images unpinned undercuts all of them.

WHY A DENYLIST OF REPOS rather than a rule about tag shape. "Is this tag an exact release?"
has no textual answer — `2.9` is a stream for influxdb and `v1.7.8` is exact for crowdsec, and
nothing in the string says which. Naming the base images is precise, and the list is short
because base images are shared by construction. Adding a repo here is a tightening; removing
one needs a better reason than "a new pin used a bare tag".

THE TAG STAYS ALONGSIDE THE DIGEST (`repo:tag@sha256:...`). That is the repo's existing pin
shape and it is load-bearing for Renovate: its k8s-defaults custom manager captures
`currentDigest` as an OPTIONAL group after `currentValue`, so a bare `repo@sha256:` would
freeze the pin with no update signal. See renovate.json's k8s-defaults manager and the
`matchUpdateTypes: [digest]` rule that auto-merges these after a 3-day soak.

Run: uv run pytest ansible/tests/k8s/test_base_images_digest_pinned.py
"""

import re

import pytest
from _helpers import ANSIBLE


# Every file the Renovate k8s-defaults manager reads, so a pin this test covers is a pin
# Renovate can bump. Keeping the two sets identical is the point: a pin outside the manager's
# patterns has no update signal, and one outside this glob has no pinning requirement.
PIN_FILE_GLOBS = (
    "roles/k8s/*/defaults/main.yml",
    "roles/setup/*/defaults/main.yml",
    "inventory/group_vars/all.yml",
)

# Repos whose tags name a stream rather than a release, so upstream re-pushes them.
BASE_IMAGE_REPOS = frozenset(
    {
        "alpine",
        "busybox",
        "influxdb",
        "nginxinc/nginx-unprivileged",
        "python",
        "zenika/alpine-chrome",
    }
)

# Mirrors renovate.json's k8s-defaults `matchStrings` entry: repo, tag, optional digest.
# Deliberately the same shape, so a pin one of them can see is a pin the other can see too.
IMAGE_RE = re.compile(
    r"^\s*(?P<var>[a-z0-9_]*_image):\s*[\"']?"
    r"(?P<repo>[^:\s\"'@]+):(?P<tag>[^\s\"'@]+)"
    r"(?:@(?P<digest>sha256:[a-f0-9]+))?[\"']?"
)


def _pin_files():
    for glob in PIN_FILE_GLOBS:
        yield from sorted(ANSIBLE.glob(glob))


def _unpinned(text):
    """Return (repo, tag) for every base-image pin in `text` carrying no digest."""
    found = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = IMAGE_RE.match(line)
        if not m:
            continue
        if m.group("repo") in BASE_IMAGE_REPOS and not m.group("digest"):
            found.append((m.group("repo"), m.group("tag")))
    return found


def test_every_base_image_pin_carries_a_digest():
    offenders = []
    for path in _pin_files():
        for repo, tag in _unpinned(path.read_text()):
            offenders.append(f"{path.relative_to(ANSIBLE)}: {repo}:{tag}")
    assert not offenders, (
        "These base-image pins carry a mutable tag and no digest, so the bytes they resolve to "
        "can change between the staging gate's run and prod's:\n  "
        + "\n  ".join(offenders)
    )


# Bare pins whose digest Renovate has already computed but cannot deliver yet. Renovate puts a
# dep's pinDigest in the same branch as its version bump, so the digest lands when that bump
# merges, and each bump below is held by a rule in renovate.json. Measured 2026-10-10 on the
# Dependency Dashboard (#3): every one lists its current tag as an update, which is the pin.
# The test below fails once an entry gains its digest, so the list only shrinks.
PENDING_DIGEST_PINS = {
    # "Awaiting Schedule": the meilisearch rule runs before 6am on Mondays.
    "karakeep_k8s_meili_image": "meilisearch manual-upgrade branch, Monday schedule",
    # "Pending Status Checks": a Docker Hub version bump soaks 7 days.
    "observability_k8s_collector_image": "otel collector version bump, 7-day soak",
    # "Awaiting Schedule".
    "observability_k8s_tempo_image": "grafana/tempo version bump, awaiting schedule",
}


def _bare_pins(text):
    """Return (var, repo, tag) for every external image pin in `text` carrying no digest.

    A built image's pin starts with `{{ k8s_registry_pull_host }}`, which IMAGE_RE cannot
    match, so in-cluster builds are out of scope by construction.
    """
    found = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = IMAGE_RE.match(line)
        if m and not m.group("digest"):
            found.append((m.group("var"), m.group("repo"), m.group("tag")))
    return found


def _all_bare_pins():
    return {
        var: f"{path.relative_to(ANSIBLE)}: {repo}:{tag}"
        for path in _pin_files()
        for var, repo, tag in _bare_pins(path.read_text())
    }


def test_every_external_image_pin_carries_a_digest():
    offenders = {
        var: where
        for var, where in _all_bare_pins().items()
        if var not in PENDING_DIGEST_PINS
    }
    assert not offenders, (
        "These image pins carry a tag and no digest. Renovate's pinDigests should have "
        "proposed one; find out why it did not, rather than adding the pin to "
        "PENDING_DIGEST_PINS:\n  " + "\n  ".join(sorted(offenders.values()))
    )


def test_pending_digest_pins_are_still_bare():
    """An entry whose pin gained a digest, or went away, must leave the list."""
    stale = sorted(set(PENDING_DIGEST_PINS) - set(_all_bare_pins()))
    assert not stale, (
        "Delete these from PENDING_DIGEST_PINS; each now carries a digest or no longer "
        "exists: " + ", ".join(stale)
    )


def test_the_scan_finds_at_least_one_pin():
    """A regex that matches nothing would pass the test above for the wrong reason."""
    seen = 0
    for path in _pin_files():
        for line in path.read_text().splitlines():
            if not line.lstrip().startswith("#") and IMAGE_RE.match(line):
                seen += 1
    assert seen > 20, (
        f"the image-pin regex matched only {seen} lines; it has stopped matching"
    )


# ── red proofs: the rule must reject as well as accept ───────────────────────────────────────
# Named `..._is_clean` / `..._is_flagged` in pairs, following
# scripts/validate/tests/test_validate_compose_templates.py. A guard observed only from the passing
# side is indistinguishable from one that fires on nothing.


@pytest.mark.parametrize(
    "line",
    [
        "seed_volume_image: alpine:3.24",
        "monitor_bridge_k8s_image: python:3.14-alpine",
        "k3s_longhorn_restore_drill_image: busybox:stable",
        "scrutiny_k8s_influxdb_image: influxdb:2.9",
        "karakeep_k8s_chrome_image: zenika/alpine-chrome:124  # trailing comment",
        '  docs_k8s_image: "nginxinc/nginx-unprivileged:1.29-alpine"',
    ],
)
def test_bare_base_image_tag_is_flagged(line):
    assert _unpinned(line), f"should have been flagged as unpinned: {line}"


@pytest.mark.parametrize(
    "line",
    [
        "seed_volume_image: alpine:3.24@sha256:" + "a" * 64,
        "karakeep_k8s_chrome_image: zenika/alpine-chrome:124@sha256:"
        + "b" * 64
        + "  # ok",
        # Not a base image: an exact upstream release needs no digest to be reproducible.
        "traefik_k8s_image: traefik:v3.7.11",
        "authelia_k8s_image: authelia/authelia:4.39.20",
        # A commented-out pin is documentation, not a deployed image.
        "# seed_volume_image: alpine:3.24",
        # Locally built images resolve through the cluster registry, where a digest is
        # meaningless: the tag is rewritten by image-builder on every build.
        'code_server_k8s_image: "{{ k8s_registry_pull_host }}/code-server:latest"',
    ],
)
def test_pinned_or_exempt_image_is_clean(line):
    assert not _unpinned(line), f"should not have been flagged: {line}"


def test_bare_application_pin_is_flagged_by_the_fleet_rule():
    assert _bare_pins("authelia_k8s_image: authelia/authelia:4.39.20") == [
        ("authelia_k8s_image", "authelia/authelia", "4.39.20")
    ]


@pytest.mark.parametrize(
    "line",
    [
        "authelia_k8s_image: authelia/authelia:4.39.20@sha256:" + "c" * 64,
        'code_server_k8s_image: "{{ k8s_registry_pull_host }}/code-server:latest"',
        "# authelia_k8s_image: authelia/authelia:4.39.20",
    ],
)
def test_digest_pinned_or_built_image_is_clean_for_the_fleet_rule(line):
    assert not _bare_pins(line), f"should not have been flagged: {line}"
