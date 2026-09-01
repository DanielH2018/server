#!/usr/bin/env python3
"""Shared base images must carry an `@sha256:` digest, not a tag alone.

WHY THIS EXISTS. A tag is mutable: the publisher can re-push it with new bytes and every
consumer silently changes on its next pull. For an application image that barely matters here,
because the tags this repo pins are exact upstream releases (`traefik:v3.7.11`,
`lscr.io/linuxserver/sonarr:4.0.17.2952-ls312`) and a publisher re-pushing one of those is
rare and newsworthy. The base images are the opposite case on both counts. `alpine:3.24` and
`python:3.14-alpine` name a patch *stream*, so upstream re-pushes them as a matter of routine,
and they are the init containers, probes and sidecars nobody watches — 20 of the 56 tag-only
references on the k8s plane, measured 2026-08-29.

WHAT THAT COSTS, concretely. `gitops_deploy_staging_gate` asks daniel-stage about a COMMIT and
then deploys prod. A base image re-pushed between the staging run and the prod run is invisible
to that gate: staging validated one set of bytes and prod ran another, and nothing in the
pipeline can tell. The gate's guarantee is only as strong as the weakest pin in the manifests
it validates, so leaving the most-frequently-re-pushed images unpinned undercuts the whole
mechanism.

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

Run: uv run pytest ansible/tests/test_base_images_digest_pinned.py
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
    r"^\s*[a-z0-9_]*_image:\s*[\"']?"
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
# scripts/validate/test_validate_compose_templates.py. A guard observed only from the passing
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
