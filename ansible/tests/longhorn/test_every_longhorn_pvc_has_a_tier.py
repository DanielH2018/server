#!/usr/bin/env python3
"""Guards that every `storageClassName: longhorn` PVC is routed to a backup tier.

`homelab/navidrome-data` (added 2026-09-02, #864) landed in none of the three routing
lists in `ansible/roles/setup/k3s/defaults/main.yml`, so `longhorn.yml:436-439` labelled
it into recurring group `default` — daily backups to B2, retain 14 — against the
weekly-only policy in `docs/longhorn-backup-tiering.md:27-31` (issue #946). Nothing
asserted that every bound `longhorn`-class PVC appears in one of the three lists;
`test_longhorn_storageclass.py::test_every_routed_volume_is_a_real_pvc` only checks the
list -> PVC direction, so a PVC a role declares and no list names is invisible to it.

The class check is EXACT (`== "longhorn"`, never a substring): `longhorn-nobackup` is a
real, separate StorageClass 13 PVCs use to opt out of the backup plane structurally, and
a substring match would demand list membership for every one of them.

Namespace comes off the rendered PVC document or the resolved `k8s_namespace` Jinja
context, never a hardcoded literal — `test_longhorn_storageclass.py:178` hardcodes
`homelab`, which would silently miss the `observability` namespace's PVCs (claude-otel's
prometheus/loki/tempo/grafana volumes) were any of them ever moved onto `longhorn`.

Run: uv run pytest ansible/tests/longhorn/test_every_longhorn_pvc_has_a_tier.py
"""

import yaml

from _helpers import SETUP_ROLES
from _k8s_render import (
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    K8S_ROLES,
    SHARED_TPL,
    k8s_entries,
    load_yaml,
    make_env,
    rendered_docs,
    resolve_vars,
    role_defaults,
)

K3S = SETUP_ROLES / "k3s"
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
# through both mechanisms below (its own template, and the shared volume-claim role) rather
# than passing vacuously because a glob or a role-name check stopped matching. Pick real,
# stable members — these have not moved tiers since the lists existed.
_KNOWN_LONGHORN_PVCS = frozenset(
    {
        "homelab/navidrome-data",  # volume-claim role, weekly tier (this PR)
        "homelab/sonarr-config",  # volume-claim role, weekly tier
        "homelab/traefik-acme",  # own template, R2 tier
        "homelab/crowdsec-db",  # own template, nobackup tier
    }
)


def _k3s_defaults() -> dict:
    return yaml.safe_load((K3S / "defaults" / "main.yml").read_text())


def _base_context() -> dict:
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), "playbook_dir": str(ANSIBLE)}
    return resolve_vars(base, base)


def _rendered_longhorn_pvcs() -> set[str]:
    """`namespace/name` for every PVC a role renders directly, on the `longhorn` class.

    Covers roles that own their PVC manifest (traefik-acme, crowdsec-db, n8n-files, ...).
    Namespace is read off the rendered document, never assumed.
    """
    found = set()
    for _role, _tpl, doc in rendered_docs():
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
            found.add(f"{namespace}/{name}")
    return found


def _volume_claim_longhorn_pvcs(base: dict) -> set[str]:
    """`namespace/name` for every PVC the shared `k8s/volume-claim` role creates.

    `k8s/volume-claim` is never rendered under its own role (it has no `container_item` /
    `containers_list` entry) — its `templates/pvc.yaml.j2` only ever renders with the vars a
    calling role passes on its `ansible.builtin.include_role` task, e.g. sonarr's
    `volume_claim_name: "{{ sonarr_k8s_claim }}"` and
    `volume_claim_storage_class: "{{ sonarr_k8s_storage_class }}"`.

    Both `volume_claim_name` and `volume_claim_storage_class` are rendered with Jinja
    against the calling role's own context, rather than guessed by stripping `_claim` and
    appending `_storage_class` from the name var: that naming convention holds for most
    callers but not autokuma-data, whose task passes uptime-kuma's PARENT
    `uptime_kuma_k8s_storage_class` var, not a sibling `uptime_kuma_k8s_autokuma_storage_class`
    that does not exist. Rendering the actual expression handles that caller without a
    special case.

    `volume-claim`'s own defaults are merged in UNDER the calling role's context (Ansible's
    real precedence: role defaults are the weakest layer), not just used as a naming guess.
    terraria-stats's `include_role` passes `volume_claim_name` but never overrides
    `volume_claim_storage_class`, so it inherits volume-claim's own default (`longhorn`) —
    without this merge that caller's storage class would resolve to nothing and its PVC
    would silently drop out of the census.
    """
    entries = k8s_entries()
    env = make_env([SHARED_TPL])
    volume_claim_defaults = role_defaults("volume-claim", base)
    found = set()
    for role_dir in sorted(d for d in K8S_ROLES.iterdir() if d.is_dir()):
        role = role_dir.name
        tasks_dir = role_dir / "tasks"
        if role not in entries or not tasks_dir.is_dir():
            continue
        ctx = {
            **base,
            **volume_claim_defaults,
            **role_defaults(role, base),
            "container_item": entries[role],
        }
        namespace = ctx.get("k8s_namespace", "homelab")
        for task_file in sorted(tasks_dir.glob("*.yml")):
            try:
                tasks = yaml.safe_load(task_file.read_text())
            except yaml.YAMLError:
                continue
            if not isinstance(tasks, list):
                continue
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                inc = task.get("ansible.builtin.include_role")
                if not isinstance(inc, dict) or inc.get("name") != "k8s/volume-claim":
                    continue
                task_vars = task.get("vars") or {}
                name_expr = task_vars.get(
                    "volume_claim_name", "{{ volume_claim_name }}"
                )
                class_expr = task_vars.get(
                    "volume_claim_storage_class", "{{ volume_claim_storage_class }}"
                )
                if not isinstance(name_expr, str) or not isinstance(class_expr, str):
                    continue
                try:
                    name = env.from_string(name_expr).render(ctx)
                    storage_class = env.from_string(class_expr).render(ctx)
                except Exception:  # noqa: S112 -- unresolvable under this stub context, not a finding
                    continue
                if storage_class == _LONGHORN_CLASS:
                    found.add(f"{namespace}/{name}")
    return found


def _longhorn_class_pvcs() -> set[str]:
    """Every `namespace/name` PVC declared with storageClassName EXACTLY `longhorn`."""
    base = _base_context()
    return _rendered_longhorn_pvcs() | _volume_claim_longhorn_pvcs(base)


def _uncovered(declared: set[str], lists: dict[str, set[str]]) -> set[str]:
    """`declared` PVCs that appear in none of `lists`'s value sets."""
    covered: set[str] = set()
    for entries in lists.values():
        covered |= entries
    return declared - covered


def test_every_longhorn_pvc_has_a_tier():
    defaults = _k3s_defaults()
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
    """Non-vacuity floor: the collector must still find PVCs from both enumeration paths."""
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
