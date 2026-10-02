#!/usr/bin/env python3
"""Pod-level volume names must name their workload, and every mount must resolve.

WHY THIS EXISTS. Two separate problems, one scan, because the same scan finds both.

1. **Descriptive names.** A `volumes[].name` of `config` or `data` is pod-scoped and therefore
   legal, but it reads identically in every manifest. Whoever is looking at a `volumeMounts`
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

The scan reads each template as the deploy renders it, so a volume name that moves into a role
default is still checked as the name the pod gets rather than as `{{ ... }}`. It stays textual
rather than a YAML parse because it judges the `volumes:` and `volumeMounts:` blocks by
position, which a parse would merge.

`_k8s_render` skips the roles a caller renders and the roles with no `containers_list` entry,
so their templates fall back to the source. `SOURCE_FALLBACK_WITH_VOLUMES` names the members of
that fallback that declare a volume, and a test below holds it. A render-only census would drop
them and still read green (#3224).

Run: uv run pytest ansible/tests/k8s/test_volume_names_descriptive.py
"""

import re

import pytest
from _helpers import K8S_ROLES
from _k8s_render import rendered_texts


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


# The templates the render skips that declare a `volumes:` or `volumeMounts:` block. Each is
# checked from its source. image-builder's context ConfigMap and volume-claim's PVC are
# skipped too, but declare no volume, so they are not members.
SOURCE_FALLBACK_WITH_VOLUMES = frozenset({"image-builder/build-job.yaml.j2"})


def source_fallback() -> dict[str, str]:
    """Every role template the render does not reach, keyed `<role>/<template>`, as source."""
    rendered = {f"{role}/{name}" for role, name, _ in rendered_texts()}
    return {
        key: path.read_text()
        for path in sorted(K8S_ROLES.glob("*/templates/*.j2"))
        if (key := str(path.relative_to(K8S_ROLES)).replace("/templates/", "/"))
        not in rendered
    }


def manifest_texts() -> dict[str, str]:
    """The census: each template's render where `_k8s_render` reaches its role, else its source.

    Rendered entries include the shared defaults from `ansible/templates/` that a role picks up
    for a manifest it ships no template for, since the pod reads those too. A macro-only file
    such as pihole's `pihole-deployment.yaml.j2` renders empty; its volumes are checked in the
    `deployment.yaml.j2` and `deployment-2.yaml.j2` renders that call it.
    """
    texts = {f"{role}/{name}": text for role, name, text in rendered_texts()}
    return texts | source_fallback()


MANIFESTS = manifest_texts()


def volume_blocks(text: str) -> list[tuple[str, int, list[str]]]:
    """Every volumes/volumeMounts block as (kind, 1-indexed line, names in order).

    Scoped to top-level list items of the block so that nested keys — an env var, a port, a
    container — are never collected. Those are contracts with the image or with an external
    referrer and must not be swept up by a naming pass.

    Names are kept per block and in order, not merged into a set, because a duplicate name
    within one block is its own defect and set-merging is exactly what hides it.
    """
    lines = text.splitlines()
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


def volume_names(text: str) -> dict[str, set[str]]:
    """The distinct names per kind, merged across every block in the file."""
    found: dict[str, set[str]] = {"volumes": set(), "volumeMounts": set()}
    for kind, _, names in volume_blocks(text):
        found[kind].update(names)
    return found


@pytest.mark.parametrize("key", sorted(MANIFESTS))
def test_every_mount_resolves_to_a_declared_volume(key: str) -> None:
    found = volume_names(MANIFESTS[key])
    orphans = sorted(found["volumeMounts"] - found["volumes"])
    assert not orphans, (
        f"{key} mounts volume(s) it never declares: {orphans}. "
        "The pod is rejected at admission; no schema check sees this."
    )


@pytest.mark.parametrize("key", sorted(MANIFESTS))
def test_no_declared_volume_is_unmounted(key: str) -> None:
    found = volume_names(MANIFESTS[key])
    unused = sorted(found["volumes"] - found["volumeMounts"])
    assert not unused, (
        f"{key} declares volume(s) nothing mounts: {unused}. "
        "Usually the leftover half of a rename."
    )


@pytest.mark.parametrize("key", sorted(MANIFESTS))
def test_no_volumes_block_repeats_a_name(key: str) -> None:
    """Two `volumes:` entries sharing a name — the way a rename collides two volumes into one.

    Nothing else catches it: comparing volumes against volumeMounts as sets passes, and
    `check yaml` flags duplicate mapping keys, not a repeated `name:` across list items.

    `volumeMounts` is deliberately excluded. Repeating a name there is legal and used on
    purpose — code-server mounts `code-server-workspace` at three paths by `subPath`, and
    jellyfin mounts `media` twice. The illegal duplicate on that side is a repeated
    `mountPath`, which is not a naming question.
    """
    for kind, line, names in volume_blocks(MANIFESTS[key]):
        if kind != "volumes":
            continue
        dupes = sorted({n for n in names if names.count(n) > 1})
        assert not dupes, (
            f"{key}:{line} declares volume(s) {dupes} more than once. "
            "The pod is rejected at admission."
        )


@pytest.mark.parametrize("key", sorted(MANIFESTS))
def test_volume_names_name_their_workload(key: str) -> None:
    names = (
        volume_names(MANIFESTS[key])["volumes"]
        | volume_names(MANIFESTS[key])["volumeMounts"]
    )
    generic = sorted(n for n in names - ALLOWED_UNPREFIXED if n in GENERIC)
    assert not generic, (
        f"{key} uses generic volume name(s): {generic}. "
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


def test_the_scanner_reads_past_a_jinja_tag() -> None:
    """A conditional volume must not end the block, or every entry after it reads undeclared."""
    assert volume_names(_CONDITIONAL_VOLUMES)["volumes"] == {
        "traefik-acme",
        "traefik-tmp",
    }


def test_the_scanner_still_sees_an_orphan_across_a_jinja_tag() -> None:
    """The rejecting half: skipping tags must not also skip the defect the guard exists for."""
    found = volume_names(
        _CONDITIONAL_VOLUMES
        + "      volumeMounts:\n"
        + "{% if manage_acme %}\n"
        + "        - name: traefik-nonexistent\n"
        + "          mountPath: /data\n"
        + "{% endif %}\n"
    )
    assert found["volumeMounts"] - found["volumes"] == {"traefik-nonexistent"}


def test_the_source_fallback_is_only_the_templates_the_render_skips() -> None:
    """The fallback reads source for exactly the volume-bearing templates no render reaches.

    A template that joins the set unannounced is one the render stopped reaching, which may
    hide a name a role default supplies. A member that leaves it is one the render now
    reaches, so its fallback entry is dead.
    """
    with_volumes = {
        key for key, text in source_fallback().items() if volume_blocks(text)
    }
    assert with_volumes == SOURCE_FALLBACK_WITH_VOLUMES


def test_a_reached_role_is_checked_from_its_render() -> None:
    """A name a role default supplies arrives resolved, the point of reading renders.

    artifacts keys its volume on `artifacts_k8s_node` and on each peer's name, which a source
    scan reads as `artifacts-{{ ... }}`.
    """
    names = volume_names(MANIFESTS["artifacts/deployment.yaml.j2"])["volumes"]
    assert {"artifacts-daniel-box", "artifacts-daniel-server"} <= names
    assert not any("{{" in name for name in names)
