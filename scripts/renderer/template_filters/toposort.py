"""Orders the k8s services.

`toposort_containers` orders the renderer's entries by the map `build_k8s_dep_map` derives from
each role's templates and entry. Every entry is a k8s service; the Pi's services deploy from
`compose_services` in its package.
"""

import heapq
import os
from graphlib import CycleError, TopologicalSorter


class DependencyCycle(ValueError):
    """Entries whose dependencies form a cycle, so no order exists."""


def toposort_containers(containers_list, deps_map):
    """Topologically sort containers_list by a name -> [dependency names] map.

    Stable: ties within a topological level preserve the original list order.
    Deps not present in containers_list are silently ignored.
    Raises DependencyCycle if a dependency cycle is detected, and ValueError if two
    entries share a name.
    """
    position = {c["name"]: i for i, c in enumerate(containers_list)}
    repeated = sorted(
        {c["name"] for i, c in enumerate(containers_list) if position[c["name"]] != i}
    )
    if repeated:
        raise ValueError(f"Duplicate names in containers: {', '.join(repeated)}")
    sorter = TopologicalSorter(
        {
            name: sorted(dep for dep in deps_map.get(name, []) if dep in position)
            for name in position
        }
    )
    try:
        sorter.prepare()
    except CycleError as error:
        cycle = " -> ".join(error.args[1][::-1])
        raise DependencyCycle(f"Dependency cycle in containers: {cycle}") from error
    heap = [position[name] for name in sorter.get_ready()]
    heapq.heapify(heap)
    result = []
    while heap:
        entry = containers_list[heapq.heappop(heap)]
        result.append(entry)
        sorter.done(entry["name"])
        for name in sorter.get_ready():
            heapq.heappush(heap, position[name])
    return result


# A role writes `apiVersion: traefik.io/...` directly or via the shared `ingressroute.yml.j2`
# macro. This scan alone is not enough: routes rendered from `k8s/templates/` leave no string in the
# role, so the entry's `hostname` is the first test.
_TRAEFIK_CRD_MARKERS = ("traefik.io", "ingressroute.yml.j2")

# Roles exempted from the derived "renders a Traefik CRD -> depends on traefik" edge.
# A written reason, not a bare set, for the same cause the position-based test it replaces
# gave for CRD_ORDER_EXEMPT: the whole failure mode here is an ordering decision that has to
# survive as more than a comment someone can outrun.
K8S_CRD_EDGE_EXEMPT = {
    "crowdsec": (
        "crowdsec must precede traefik for the LAPI machine credential (declared as "
        "traefik's depends_on in host_vars) -- a traefik-before-crowdsec edge here would "
        "cycle against that one. The accepted cost: crowdsec's own IngressRoute (the LAPI's "
        "LAN face) applies before traefik installs the Traefik CRDs it needs, which is "
        "harmless on a running cluster and a documented first-run-only failure on a rebuild."
    ),
}


# An edge onto every other entry, not list position; a role depending on one of these makes a cycle
# and `toposort_containers` raises.
K8S_APPLIES_LAST = {
    "netpol-baseline": (
        "its first task drains the rollouts queued earlier in its batch, then fences the "
        "namespace and probes the fence. The drain cannot wait for a role deployed after it, "
        "so every workload role must come before it (docs/networkpolicy-slice-answers.md)."
    ),
}


def _entry_is_routed(container):
    """Whether a containers_list entry declares a Traefik route.

    The `hostname` key is the declaration: `ingressroute()` takes it as the host label, and an
    entry carrying one is a service someone reaches through the edge. It is the only routed
    test that survives a route rendered from `k8s/templates/ingressroute-default.yaml.j2`,
    which leaves nothing in the role for `_role_renders_traefik_crd` to find.

    It also claims an edge the templates never did, for a role whose route lives in traefik's
    own file provider rather than in an IngressRoute of its own -- livesync is the one, and its
    CLAUDE.md already states the dependency. An edge too many costs an ordering constraint that
    was already true; one too few applies a Traefik CRD before traefik owns the CRDs.
    """
    return bool(container.get("hostname"))


def _role_renders_traefik_crd(role_templates_dir):
    """Whether a k8s role's templates render a traefik.io object (see _TRAEFIK_CRD_MARKERS)."""
    try:
        names = os.listdir(role_templates_dir)
    except OSError:
        return False
    for name in names:
        if not name.endswith(".j2"):
            continue
        try:
            with open(os.path.join(role_templates_dir, name), encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        if any(marker in text for marker in _TRAEFIK_CRD_MARKERS):
            return True
    return False


def build_k8s_dep_map(containers_list, k8s_roles, applies_last=None):
    """Derive the k8s roles' dependency map.

    There is no hand-authored per-role dependency file, and no constraint rests on list
    position. Three rules are derived, which is what lets a new role's edges arrive for free:

      * every routed role depends on traefik, except K8S_CRD_EDGE_EXEMPT -- routed meaning the
        entry declares a `hostname`, or the role's own templates render a Traefik CRD
      * every entry with `use_authelia: true` depends on authelia
      * each K8S_APPLIES_LAST role (netpol-baseline) depends on every other entry outside
        that set, so it applies last wherever the list puts it

    Every other constraint is declared data: an explicit `depends_on:` list on the
    containers_list entry, unioned in below. traefik declares crowdsec, which no template
    carries; configarr declares sonarr and radarr, whose bare Service names it dials; and
    jellyfin and tdarr declare dri-device-plugin, which advertises the `devic.es/dri`
    resource they request.

    `k8s_roles` is the directory holding the k8s roles, whose templates the routed rule reads.

    Takes the FULL containers_list, never a `--role` subset. `cluster.bundle_ordering`
    generates every tag's `dependsOn` from this map, so a map built from a subset would give
    each role outside it no edges, and a tag's edges would depend on what else rendered.

    `applies_last` replaces K8S_APPLIES_LAST when given. The renderer never passes it; a test
    passes an empty dict to show the edge goes away with the dict.
    """
    applies_last = K8S_APPLIES_LAST if applies_last is None else applies_last
    dep_map = {}
    names = [c["name"] for c in containers_list]
    for c in containers_list:
        name = c["name"]
        deps = set(c.get("depends_on", []))
        if name in applies_last:
            deps.update(n for n in names if n not in applies_last)
        if c.get("use_authelia") and name != "authelia":
            deps.add("authelia")
        if name != "traefik" and name not in K8S_CRD_EDGE_EXEMPT:
            role_templates = os.path.join(k8s_roles, name, "templates")
            if _entry_is_routed(c) or _role_renders_traefik_crd(role_templates):
                deps.add("traefik")
        dep_map[name] = sorted(deps)
    return dep_map
