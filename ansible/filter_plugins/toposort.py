"""Ansible filter plugin for ordering containers_list by declared role_deps.

Loads each container's meta/deps.yml, builds a dependency map, and exposes filters that
topologically sort containers_list, find the transitive-dependency closure of a tagged
deploy, and expand a tagged deploy to include dependencies that are not already running.
"""

from __future__ import annotations
import heapq
import os
import yaml
from ansible.errors import AnsibleFilterError


def _tags(container):
    """Effective deploy tags for a containers_list entry.

    Defaults to [name] so host_vars don't have to repeat `tags: [<name>]`; an explicit `tags:`
    overrides.
    """
    return container.get("tags", [container["name"]])


def build_dep_map(containers_list, playbook_dir, requested_tags):
    """Build the role dependency map, loading only relevant deps.yml files.

    For full deploys ('all' in tags): reads every container's deps.yml.
    For tagged deploys: starts from the requested containers and expands
    transitively, so only deps.yml files in the closure are read.
    Non-closure containers are initialised with an empty dep list so the
    toposort still receives a complete map.
    """
    name_to_container = {c["name"]: c for c in containers_list}
    dep_map = {c["name"]: [] for c in containers_list}

    def _load(name):
        path = os.path.join(
            playbook_dir, "roles", "containers", name, "meta", "deps.yml"
        )
        try:
            with open(path) as fh:
                return (yaml.safe_load(fh) or {}).get("role_deps", [])
        except OSError, yaml.YAMLError:
            return []

    if "all" in requested_tags or not requested_tags:
        for name in name_to_container:
            dep_map[name] = _load(name)
    else:
        requested = {
            c["name"] for c in containers_list if set(_tags(c)) & set(requested_tags)
        }
        frontier = list(requested)
        loaded = set()
        while frontier:
            name = frontier.pop()
            if name in loaded or name not in name_to_container:
                continue
            loaded.add(name)
            deps = _load(name)
            dep_map[name] = deps
            frontier.extend(dep for dep in deps if dep not in loaded)

    return dep_map


def toposort_containers(containers_list, deps_map):
    """Topologically sort containers_list by their declared role_deps.

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


def dep_closure(containers_list, deps_map, requested_tags):
    """Return containers that are transitive deps of the tagged containers.

    Excludes the directly-requested containers themselves.
    Used to narrow the running-state check to only the relevant deps.
    """
    name_to_obj = {c["name"]: c for c in containers_list}
    requested = {
        c["name"] for c in containers_list if set(_tags(c)) & set(requested_tags)
    }
    all_deps = set()
    frontier = list(requested)
    while frontier:
        for dep in deps_map.get(frontier.pop(), []):
            if dep in name_to_obj and dep not in all_deps and dep not in requested:
                all_deps.add(dep)
                frontier.append(dep)
    return [c for c in containers_list if c["name"] in all_deps]


def expand_with_deps(containers_list, deps_map, requested_tags, running_names):
    """Expand a tagged deployment to include unmet dependencies.

    For each container whose tags match requested_tags, walks the dep graph
    transitively and includes upstream roles that are not already running.
    The originally-requested containers are always included regardless of
    running state. Preserves containers_list's order (the caller toposorts that
    list first, so the returned subset comes out topologically ordered).
    """
    name_to_obj = {c["name"]: c for c in containers_list}
    running = set(running_names)

    requested = {
        c["name"] for c in containers_list if set(_tags(c)) & set(requested_tags)
    }

    all_needed = set(requested)
    frontier = list(requested)
    while frontier:
        for dep in deps_map.get(frontier.pop(), []):
            if dep in name_to_obj and dep not in all_needed:
                all_needed.add(dep)
                frontier.append(dep)

    effective = {n for n in all_needed if n in requested or n not in running}
    return [c for c in containers_list if c["name"] in effective]


# A role's own IngressRoute/Middleware templates either write `apiVersion: traefik.io/...`
# directly or pull it in through the one shared `ingressroute.yml.j2` macro (`{% from
# 'ingressroute.yml.j2' import ingressroute %}`) -- there is no third way in this repo to
# emit one. A textual scan for either string matched a full Jinja render of every k8s role
# exactly (35/35 roles, see ansible/tests/deploy/test_k8s_toposort.py), so it stands in for
# the render at deploy time without paying for one.
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
    """Derive the k8s play's dependency map -- the k8s counterpart of build_dep_map.

    There is no per-role meta/deps.yml on this side. Two of the three real ordering
    constraints are mechanically derivable, and re-deriving them here (instead of reading a
    hand-authored file) is what lets a new role's edges arrive for free:

      * every role rendering a Traefik CRD depends on traefik, except K8S_CRD_EDGE_EXEMPT
      * every entry with `use_authelia: true` depends on authelia

    The third -- crowdsec before traefik -- is not derivable from a template, so it is
    declared data instead: an explicit `depends_on:` list on the containers_list entry,
    unioned in below.

    Takes the FULL containers_list, never a tag-narrowed one. Unlike the Docker play, the
    k8s play applies `--tags` per-role inside a single loop rather than narrowing the list
    before dependency resolution (no dep_closure/expand_with_deps here), so building this
    map from a tagged subset would leave every role outside that subset with an empty dep
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
            if _role_renders_traefik_crd(role_templates):
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
            "build_dep_map": build_dep_map,
            "build_k8s_dep_map": build_k8s_dep_map,
            "toposort_containers": toposort_containers,
            "dep_closure": dep_closure,
            "expand_with_deps": expand_with_deps,
            "filter_by_platform": filter_by_platform,
        }
