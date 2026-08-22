#!/usr/bin/env python3
"""Pod-level volume names must name their workload, and every mount must resolve.

WHY THIS EXISTS. Two separate problems, one check, because they are found by the same scan.

1. **Descriptive names.** A `volumes[].name` of `config` or `data` is pod-scoped and therefore
   legal, but it reads identically in 71 manifests. Whoever is looking at a `volumeMounts`
   entry in a log line, a `kubectl describe`, or a diff cannot tell which workload's config it
   is. Every cluster-scoped name in this repo is already `<service>-<purpose>` (PVCs, Services,
   ConfigMaps, Secrets, Deployments); the pod-level names were the one layer that was not, and
   this test keeps them from drifting back.

2. **Orphan mounts.** A `volumeMounts[].name` with no matching `volumes[].name` is a
   *cross-field* error. The manifest validator schema-checks fields in isolation and cannot
   see it, and `--check` skips the apply entirely, so the first thing that catches it is the
   live API server. A rename that touches one of the pair and not the other produces exactly
   this, which makes it the check that matters most during a naming pass.

The scan is deliberately textual, not a YAML parse: these are Jinja templates, and rendering
them needs the full inventory. Volume names never contain Jinja except where the name is keyed
on a node (`artifacts-{{ artifacts_k8s_node }}`), which is descriptive by construction.

Run: uv run pytest ansible/tests/test_volume_names_descriptive.py
"""

import re
from pathlib import Path

import pytest

K8S_ROLES = Path(__file__).resolve().parents[1] / "roles" / "k8s"

BLOCK_RE = re.compile(r"^(\s*)(volumes|volumeMounts):\s*$")
NAME_RE = re.compile(r"^(\s*)- name:\s*(\S.*?)\s*$")

# Names that say nothing about which workload owns the volume. Adding one here is a
# tightening; removing one needs a reason better than "a new manifest used it".
GENERIC = frozenset(
    {
        "app-config",
        "cache",
        "conf",
        "config",
        "configs",
        "context",
        "credentials",
        "data",
        "db",
        "files",
        "log",
        "logs",
        "positions",
        "repos",
        "run",
        "script",
        "secrets",
        "seed",
        "server",
        "settings",
        "temp",
        "tmp",
        "token",
        "work",
        "workspace",
    }
)

# `media` is the shared media library, mounted by six roles from the one `media-data` claim.
# It names a stack rather than a single workload, which is the point of it.
ALLOWED_UNPREFIXED = frozenset({"media"})


def manifest_templates() -> list[Path]:
    return sorted(K8S_ROLES.glob("*/templates/*.j2"))


def volume_names(path: Path) -> dict[str, set[str]]:
    """Return the `- name:` entries directly under each volumes/volumeMounts block.

    Scoped to top-level list items of the block so that nested keys — an env var, a port, a
    container — are never collected. Those are contracts with the image or with an external
    referrer and must not be swept up by a naming pass.
    """
    lines = path.read_text().splitlines()
    found: dict[str, set[str]] = {"volumes": set(), "volumeMounts": set()}
    i = 0
    while i < len(lines):
        block = BLOCK_RE.match(lines[i])
        if not block:
            i += 1
            continue
        indent, kind = len(block.group(1)), block.group(2)
        j = i + 1
        while j < len(lines):
            line = lines[j]
            if line.strip() and (len(line) - len(line.lstrip())) <= indent:
                break
            name = NAME_RE.match(line)
            if name and len(name.group(1)) <= indent + 2:
                found[kind].add(name.group(2))
            j += 1
        i = j
    return found


@pytest.mark.parametrize(
    "path", manifest_templates(), ids=lambda p: str(p.relative_to(K8S_ROLES))
)
def test_every_mount_resolves_to_a_declared_volume(path: Path) -> None:
    found = volume_names(path)
    orphans = sorted(found["volumeMounts"] - found["volumes"])
    assert not orphans, (
        f"{path.relative_to(K8S_ROLES)} mounts volume(s) it never declares: {orphans}. "
        "The pod is rejected at admission; no schema check sees this."
    )


@pytest.mark.parametrize(
    "path", manifest_templates(), ids=lambda p: str(p.relative_to(K8S_ROLES))
)
def test_no_declared_volume_is_unmounted(path: Path) -> None:
    found = volume_names(path)
    unused = sorted(found["volumes"] - found["volumeMounts"])
    assert not unused, (
        f"{path.relative_to(K8S_ROLES)} declares volume(s) nothing mounts: {unused}. "
        "Usually the leftover half of a rename."
    )


@pytest.mark.parametrize(
    "path", manifest_templates(), ids=lambda p: str(p.relative_to(K8S_ROLES))
)
def test_volume_names_name_their_workload(path: Path) -> None:
    names = volume_names(path)["volumes"] | volume_names(path)["volumeMounts"]
    generic = sorted(n for n in names - ALLOWED_UNPREFIXED if n in GENERIC)
    assert not generic, (
        f"{path.relative_to(K8S_ROLES)} uses generic volume name(s): {generic}. "
        "Name the volume for the workload or component that owns it — "
        "`sonarr-config`, not `config`."
    )
