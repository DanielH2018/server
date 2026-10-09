#!/usr/bin/env python3
"""Every Longhorn PVC size must be an integer multiple of the backup block size.

Longhorn's admission webhook enforces this, and it matters because
`default-backup-block-size` is 16 MiB rather than 2 MiB. A 100Mi PVC is a multiple of 2 MiB and
not of 16 MiB, so the webhook refuses it:

    admission webhook "validator.longhorn.io" denied the request: volume size 104857600 must
    be an integer multiple of the backup block size 16777216

The failure mode is the dangerous part. Existing volumes are untouched — the constraint is
checked at *creation*. So a violating PVC keeps serving indefinitely and only fails when
something recreates it: a node rebuild, a restore, a disaster-recovery bring-up. That is
precisely the path the 16 MiB change exists to make survivable, so a silent violation here
converts a cost optimisation into a recovery failure.

The census reads the RENDERED manifests (#3209). Reading the templates meant resolving each
`storage: {{ var }}` against a hand-merged map of role defaults and group_vars, and reading the
`pvc()` macro's third argument with a regex because a claim built through that macro (folded
into `ansible/templates/claim-default.yaml.j2` by #3387) had no `storage:` line of its own. The render resolves both —
Ansible's own context resolves the variable, and the macro expands — so the var map, the
macro-call regex and the floor assertion that watched for their resolution falling over are all
gone with their subject. What replaces them is `KNOWN_SIZED_ROLES`: a census that stops naming a
role fails there rather than passing on fewer claims.

Run: uv run pytest ansible/tests/longhorn/test_pvc_sizes_match_block_size.py
"""

import re

import pytest
from lib import yaml_fast
from _helpers import ANSIBLE
from lib.repo_paths import K3S_DEFAULTS
from _k8s_render import rendered_docs

K8S_ROLES = ANSIBLE / "roles" / "k8s"

UNITS = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
SIZE_RE = re.compile(r"^(\d+)(Ki|Mi|Gi|Ti)$")
VOLUME_KINDS = frozenset({"PersistentVolumeClaim", "PersistentVolume"})

# Roles the census must name. A render that stops reaching a role, or a `kind:` that moves,
# leaves this suite checking fewer claims and still passing. Each of these roles sizes at least
# one volume away from the 1Gi default — 128Mi, 400Gi, 20Gi — so each is a size somebody chose
# and a claim whose arithmetic is worth holding.
KNOWN_SIZED_ROLES = frozenset(
    {"media-volume", "observability", "registry", "traefik", "valheim"}
)


def block_size_bytes() -> int:
    mib = yaml_fast.safe_load(K3S_DEFAULTS.read_text())[
        "k3s_longhorn_backup_block_size"
    ]
    return int(mib) * UNITS["Mi"]


def _requested_size(doc: dict) -> str | None:
    """The storage quantity `doc` asks for: a claim's request, or a PV's capacity."""
    spec = doc.get("spec") or {}
    requests = (spec.get("resources") or {}).get("requests") or {}
    capacity = spec.get("capacity") or {}
    size = requests.get("storage") or capacity.get("storage")
    return str(size) if size is not None else None


def declared_sizes() -> list[tuple[str, str]]:
    """(where, size) for every volume the k8s roles declare."""
    return sorted(
        (f"{role}/{name}", size)
        for role, name, doc in rendered_docs()
        if isinstance(doc, dict)
        and doc.get("kind") in VOLUME_KINDS
        and (size := _requested_size(doc))
    )


def test_the_census_names_every_role_that_sizes_a_volume() -> None:
    """Guard the guard: a census that reached nothing would pass every assertion below."""
    sizes = declared_sizes()
    roles = {where.split("/")[0] for where, _ in sizes}
    assert KNOWN_SIZED_ROLES <= roles, (
        f"the render census no longer reaches {sorted(KNOWN_SIZED_ROLES - roles)} — it is "
        f"checking {len(sizes)} sizes across {sorted(roles)}"
    )
    assert len(sizes) >= 18, f"only {len(sizes)} volume sizes found: {sizes}"


@pytest.mark.parametrize(
    ("where", "size"),
    [pytest.param(w, s, id=f"{w}:{s}") for w, s in declared_sizes()],
)
def test_pvc_size_is_a_multiple_of_the_backup_block_size(where: str, size: str) -> None:
    match = SIZE_RE.match(size)
    assert match, (
        f"{where} requests {size!r}, which is not a literal quantity. Every size is literal "
        "once rendered, so an expression here means the render left a variable unresolved"
    )
    bytes_requested = int(match.group(1)) * UNITS[match.group(2)]
    block = block_size_bytes()
    assert bytes_requested % block == 0, (
        f"{where} requests {size}, which is not an integer "
        f"multiple of the {block // UNITS['Mi']}Mi backup block size. Longhorn's admission "
        "webhook refuses to create it — the existing volume keeps working, so this only "
        "fails when the volume is recreated, i.e. during a rebuild or a restore."
    )


def test_a_non_multiple_size_fails_the_rule() -> None:
    """The rejecting half: the arithmetic above must be able to refuse a size.

    100Mi is a multiple of 2Mi and not of 16Mi — the exact kind of value the webhook refuses.
    """
    assert (100 * UNITS["Mi"]) % block_size_bytes() != 0, (
        "100Mi is a multiple of the configured block size, so this reject case proves nothing "
        "— pick a size that is not, or the block size changed and this test needs rewriting."
    )
