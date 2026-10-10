#!/usr/bin/env python3
"""Guards that every `storageClassName: longhorn` PVC is routed to a backup tier.

A `longhorn`-class PVC in none of the three routing lists in
`ansible/roles/setup/k3s/defaults/main.yml` is labelled by `longhorn.yml:436-439` into
recurring group `default` — daily backups to B2, retain 14 — against the weekly-only policy in
`docs/longhorn-backup-tiering.md:27-31`. This guard asserts that every bound `longhorn`-class
PVC appears in one of the three lists;
`test_longhorn_storageclass.py::test_every_routed_volume_is_a_real_pvc` only checks the
list -> PVC direction, so a PVC a role declares and no list names is invisible to it.

The class check is EXACT (`== "longhorn"`, never a substring): `longhorn-nobackup` is a
real, separate StorageClass 13 PVCs use to opt out of the backup plane structurally, and
a substring match would demand list membership for every one of them.

Namespace comes off the rendered PVC document or the resolved `k8s_namespace` Jinja
context, never a hardcoded literal — `test_longhorn_storageclass.py:178` hardcodes
`homelab`, which would silently miss the `observability` namespace's PVCs (observability's
prometheus/loki/tempo/grafana volumes) were any of them ever moved onto `longhorn`.

Run: uv run pytest ansible/tests/longhorn/test_every_longhorn_pvc_has_a_tier.py
"""

from lib.service_tiers import resolved_tier_lists

from _helpers import load_defaults
from _k8s_render import rendered_docs
from lib.repo_paths import K3S_ROLE

_LONGHORN_CLASS = "longhorn"

# Three hand-maintained lists of `namespace/pvcName` decide where every `longhorn`-class
# PVC's backups go. See test_longhorn_storageclass.py's module-level comment for what each
# means; this module only checks that every such PVC is in one of them.
_ROUTING_LISTS = (
    "k3s_longhorn_r2_volumes",
    "k3s_longhorn_weekly_volumes",
    "k3s_longhorn_nobackup_volumes",
)

# A named floor, not just a count: proves the collector still recognises PVCs declared
# through `k8s_claims` and through a role's own template, both read rendered, rather than
# passing vacuously because a glob or a role-name check stopped matching. Pick real, stable
# members — these have not moved tiers since the lists existed. traefik-acme is the member
# whose `k8s_claims` is an expression rather than a literal list.
_KNOWN_LONGHORN_PVCS = frozenset(
    {
        "homelab/jellyfin-config",  # k8s_claims, weekly tier
        "homelab/sonarr-config",  # k8s_claims, weekly tier
        "homelab/traefik-acme",  # k8s_claims, R2 tier
        "homelab/crowdsec-db",  # k8s_claims, nobackup tier
    }
)


def _rendered_longhorn_pvcs() -> set[tuple[str, str]]:
    """`(role, namespace/name)` for every PVC a role renders directly, on the `longhorn` class.

    Covers roles that own their PVC manifest (the observability claims) and the claims
    `k8s_claims` renders (traefik-acme, the pihole pair, ...).
    Namespace is read off the rendered document, never assumed.
    """
    found = set()
    for role, _tpl, doc in rendered_docs():
        if doc.get("kind") != "PersistentVolumeClaim":
            continue
        metadata = doc.get("metadata") or {}
        name = metadata.get("name")
        namespace = metadata.get("namespace")
        storage_class = (doc.get("spec") or {}).get("storageClassName")
        if (
            isinstance(name, str)
            and isinstance(namespace, str)
            and storage_class == _LONGHORN_CLASS
        ):
            found.add((role, f"{namespace}/{name}"))
    return found


def longhorn_pvcs_by_role() -> dict[str, set[str]]:
    """The `namespace/name` PVCs on storageClassName EXACTLY `longhorn`, by declaring role."""
    pairs = _rendered_longhorn_pvcs()
    by_role: dict[str, set[str]] = {}
    for role, pvc in pairs:
        by_role.setdefault(role, set()).add(pvc)
    return by_role


def _longhorn_class_pvcs() -> set[str]:
    """Every `namespace/name` PVC declared with storageClassName EXACTLY `longhorn`."""
    return set().union(*longhorn_pvcs_by_role().values())


def _uncovered(declared: set[str], lists: dict[str, set[str]]) -> set[str]:
    """`declared` PVCs that appear in none of `lists`'s value sets."""
    covered: set[str] = set()
    for entries in lists.values():
        covered |= entries
    return declared - covered


def test_every_longhorn_pvc_has_a_tier():
    defaults = resolved_tier_lists(load_defaults(K3S_ROLE))
    lists = {name: set(defaults.get(name) or []) for name in _ROUTING_LISTS}
    declared = _longhorn_class_pvcs()
    assert len(declared) >= 25, (
        f"only found {len(declared)} longhorn-class PVCs — the collector stopped matching "
        "a template or task shape rather than the fleet actually shrinking"
    )
    uncovered = _uncovered(declared, lists)
    assert not uncovered, (
        "these PVCs are on storageClassName: longhorn but appear in none of "
        f"{_ROUTING_LISTS} — an unrouted longhorn-class volume defaults to daily backups "
        "on B2 at retain 14 (ansible/roles/setup/k3s/tasks/longhorn.yml:436-439), against "
        f"the weekly-only policy in docs/longhorn-backup-tiering.md: {sorted(uncovered)}"
    )


def test_census_finds_every_known_longhorn_pvc():
    """Non-vacuity floor: the collector must still find named `k8s_claims` PVCs."""
    declared = _longhorn_class_pvcs()
    missing = _KNOWN_LONGHORN_PVCS - declared
    assert not missing, (
        f"the census no longer finds {sorted(missing)} — a template or task shape it reads "
        "moved, not that these PVCs disappeared"
    )


def test_an_unrouted_pvc_is_named_in_the_failure():
    """Red-proof pair for test_every_longhorn_pvc_has_a_tier: a fixture PVC in no list must
    surface by name, not get silently swallowed by the coverage check.
    """
    lists = {
        "k3s_longhorn_weekly_volumes": {"homelab/sonarr-config"},
        "k3s_longhorn_r2_volumes": {"homelab/traefik-acme"},
        "k3s_longhorn_nobackup_volumes": {"homelab/crowdsec-db"},
    }
    declared = {
        "homelab/sonarr-config",
        "homelab/traefik-acme",
        "homelab/crowdsec-db",
        "homelab/mystery-data",
    }
    assert _uncovered(declared, lists) == {"homelab/mystery-data"}
