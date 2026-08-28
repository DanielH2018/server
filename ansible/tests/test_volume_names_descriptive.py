#!/usr/bin/env python3
"""Pod-level volume names must name their workload, and every mount must resolve.

WHY THIS EXISTS. Two separate problems, one scan, because the same scan finds both.

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
   this, which makes it the check that matters most during a naming pass. Its two siblings —
   an unmounted volume, and a `volumes:` block declaring one name twice — are the other two
   ways a half-applied rename lands, and both are equally invisible to a schema check.

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
        "media",
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


def volume_blocks(path: Path) -> list[tuple[str, int, list[str]]]:
    """Every volumes/volumeMounts block as (kind, 1-indexed line, names in order).

    Scoped to top-level list items of the block so that nested keys — an env var, a port, a
    container — are never collected. Those are contracts with the image or with an external
    referrer and must not be swept up by a naming pass.

    Names are kept per block and in order, not merged into a set, because a duplicate name
    within one block is its own defect and set-merging is exactly what hides it.
    """
    lines = path.read_text().splitlines()
    blocks: list[tuple[str, int, list[str]]] = []
    i = 0
    while i < len(lines):
        block = BLOCK_RE.match(lines[i])
        if not block:
            i += 1
            continue
        indent, kind = len(block.group(1)), block.group(2)
        names: list[str] = []
        j = i + 1
        while j < len(lines):
            line = lines[j]
            # A Jinja block tag sits at column 0, because lstrip_blocks is off and an indented
            # tag would leave its leading spaces in the rendered YAML. Taken as YAML that reads
            # as dedenting out of the block, so a conditional volume would end the scan and
            # every entry after it would look undeclared. Skip the tag, keep scanning.
            if line.lstrip().startswith(("{%", "{#")):
                j += 1
                continue
            if line.strip() and (len(line) - len(line.lstrip())) <= indent:
                break
            name = NAME_RE.match(line)
            if name and len(name.group(1)) <= indent + 2:
                names.append(name.group(2))
            j += 1
        blocks.append((kind, i + 1, names))
        i = j
    return blocks


def volume_names(path: Path) -> dict[str, set[str]]:
    """The distinct names per kind, merged across every block in the file."""
    found: dict[str, set[str]] = {"volumes": set(), "volumeMounts": set()}
    for kind, _, names in volume_blocks(path):
        found[kind].update(names)
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
def test_no_volumes_block_repeats_a_name(path: Path) -> None:
    """Two `volumes:` entries sharing a name — the way a rename collides two volumes into one.

    Nothing else catches it: comparing volumes against volumeMounts as sets passes, and
    `check yaml` flags duplicate mapping keys, not a repeated `name:` across list items.

    `volumeMounts` is deliberately excluded. Repeating a name there is legal and used on
    purpose — code-server mounts `code-server-workspace` at three paths by `subPath`, and
    jellyfin mounts `media` twice. The illegal duplicate on that side is a repeated
    `mountPath`, which is not a naming question.
    """
    for kind, line, names in volume_blocks(path):
        if kind != "volumes":
            continue
        dupes = sorted({n for n in names if names.count(n) > 1})
        assert not dupes, (
            f"{path.relative_to(K8S_ROLES)}:{line} declares volume(s) {dupes} more than once. "
            "The pod is rejected at admission."
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


_CONDITIONAL_VOLUMES = """\
      volumes:
{% if manage_acme %}
        - name: traefik-acme
          emptyDir: {}
{% endif %}
        - name: traefik-tmp
          emptyDir: {}
"""


def test_the_scanner_reads_past_a_jinja_tag(tmp_path: Path) -> None:
    """A conditional volume must not end the block, or every entry after it reads undeclared."""
    path = tmp_path / "deployment.yaml.j2"
    path.write_text(_CONDITIONAL_VOLUMES)
    assert volume_names(path)["volumes"] == {"traefik-acme", "traefik-tmp"}


def test_the_scanner_still_sees_an_orphan_across_a_jinja_tag(tmp_path: Path) -> None:
    """The rejecting half: skipping tags must not also skip the defect the guard exists for."""
    path = tmp_path / "deployment.yaml.j2"
    path.write_text(
        _CONDITIONAL_VOLUMES
        + "      volumeMounts:\n"
        + "{% if manage_acme %}\n"
        + "        - name: traefik-nonexistent\n"
        + "          mountPath: /data\n"
        + "{% endif %}\n"
    )
    found = volume_names(path)
    assert found["volumeMounts"] - found["volumes"] == {"traefik-nonexistent"}
