"""Ansible filter plugin for ordering and splitting containers_list.

`toposort_containers` orders the k8s play by the map `build_k8s_dep_map` derives from each
role's templates and entry, and `filter_by_platform` splits the list between the Docker and k8s
plays. The Docker play runs in containers_list order: the Pi is its only host, and its one
ordering constraint (docker-proxy before autoheal) is that list's order.
"""

import heapq
import os
from ansible.errors import AnsibleFilterError


def toposort_containers(containers_list, deps_map):
    """Topologically sort containers_list by a name -> [dependency names] map.

    Stable: ties within a topological level preserve the original list order.
    Deps not present in containers_list are silently ignored.
    Raises AnsibleFilterError if a dependency cycle is detected.
    """
    name_to_idx = {c["name"]: i for i, c in enumerate(containers_list)}
    name_to_obj = {c["name"]: c for c in containers_list}
    names = list(name_to_idx)

    in_degree = {n: 0 for n in names}
    graph = {n: [] for n in names}
    for name in names:
        for dep in deps_map.get(name, []):
            if dep in name_to_idx:
                graph[dep].append(name)
                in_degree[name] += 1

    heap = [(name_to_idx[n], n) for n in names if in_degree[n] == 0]
    heapq.heapify(heap)
    result = []
    while heap:
        _, node = heapq.heappop(heap)
        result.append(name_to_obj[node])
        for neighbor in graph[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                heapq.heappush(heap, (name_to_idx[neighbor], neighbor))

    if len(result) != len(names):
        cycled = [n for n in names if name_to_obj[n] not in result]
        raise AnsibleFilterError(f"Dependency cycle detected in containers: {cycled}")
    return result


# A role's own IngressRoute/Middleware templates either write `apiVersion: traefik.io/...`
# directly or pull it in through the one shared `ingressroute.yml.j2` macro (`{% from
# 'ingressroute.yml.j2' import ingressroute %}`) -- there is no third way in this repo to
# emit one from a file inside the role.
#
# This scan is the SECOND of the two routed tests below, not the only one. It is not sufficient
# alone: 16 roles' IngressRoute is rendered from `ansible/templates/`, and a route rendered
# from there leaves no string in the role, so a scan-only derivation would drop the traefik
# edge for all 16 and apply their routes before the CRDs exist. The entry's `hostname` is the
# first test and the one that covers them.
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


def _entry_is_routed(container):
    """Whether a containers_list entry declares a Traefik route.

    The `hostname` key is the declaration: `ingressroute()` takes it as the host label, and an
    entry carrying one is a service someone reaches through the edge. It is the only routed
    test that survives a route rendered from `ansible/templates/ingressroute-default.yaml.j2`,
    which leaves nothing in the role for `_role_renders_traefik_crd` to find (#3043).

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


def build_k8s_dep_map(containers_list, playbook_dir):
    """Derive the k8s play's dependency map.

    There is no hand-authored per-role dependency file. Two of the three real ordering
    constraints are mechanically derivable, and re-deriving them here (instead of reading a
    hand-authored file) is what lets a new role's edges arrive for free:

      * every routed role depends on traefik, except K8S_CRD_EDGE_EXEMPT -- routed meaning the
        entry declares a `hostname`, or the role's own templates render a Traefik CRD
      * every entry with `use_authelia: true` depends on authelia

    The third -- crowdsec before traefik -- is not derivable from a template, so it is
    declared data instead: an explicit `depends_on:` list on the containers_list entry,
    unioned in below.

    Takes the FULL containers_list, never a tag-narrowed one. The k8s play applies `--tags`
    per-role inside a single loop rather than narrowing the list before dependency
    resolution, so building this map from a tagged subset would leave every role outside that subset with an empty dep
    list -- silently falling back to list order for exactly the roles a tagged deploy is
    most likely to append one after.
    """
    dep_map = {}
    for c in containers_list:
        name = c["name"]
        deps = set(c.get("depends_on", []))
        if c.get("use_authelia") and name != "authelia":
            deps.add("authelia")
        if name != "traefik" and name not in K8S_CRD_EDGE_EXEMPT:
            role_templates = os.path.join(
                playbook_dir, "roles", "k8s", name, "templates"
            )
            if _entry_is_routed(c) or _role_renders_traefik_crd(role_templates):
                deps.add("traefik")
        dep_map[name] = sorted(deps)
    return dep_map


def filter_by_platform(containers_list, platform="docker"):
    """Select containers_list entries targeting a given deploy platform.

    A missing `platform` key means "docker". That default is load-bearing:
    every pre-migration entry omits the key, so defaulting any other way would
    silently drop every service from the next deploy.
    """
    return [c for c in containers_list if c.get("platform", "docker") == platform]


class FilterModule:
    """Ansible filter plugin registering this module's containers_list helpers."""

    def filters(self):
        return {
            "build_k8s_dep_map": build_k8s_dep_map,
            "toposort_containers": toposort_containers,
            "filter_by_platform": filter_by_platform,
        }
