from __future__ import annotations
import heapq
import os
import yaml
from ansible.errors import AnsibleFilterError


def _tags(container):
    """Effective deploy tags for a containers_list entry: defaults to [name] so
    host_vars don't have to repeat `tags: [<name>]`; an explicit `tags:` overrides."""
    return container.get('tags', [container['name']])


def build_dep_map(containers_list, playbook_dir, requested_tags):
    """Build the role dependency map, loading only relevant deps.yml files.

    For full deploys ('all' in tags): reads every container's deps.yml.
    For tagged deploys: starts from the requested containers and expands
    transitively, so only deps.yml files in the closure are read.
    Non-closure containers are initialised with an empty dep list so the
    toposort still receives a complete map.
    """
    name_to_container = {c['name']: c for c in containers_list}
    dep_map = {c['name']: [] for c in containers_list}

    def _load(name):
        path = os.path.join(playbook_dir, 'roles', 'containers', name, 'meta', 'deps.yml')
        try:
            with open(path) as fh:
                return (yaml.safe_load(fh) or {}).get('role_deps', [])
        except (OSError, yaml.YAMLError):
            return []

    if 'all' in requested_tags or not requested_tags:
        for name in name_to_container:
            dep_map[name] = _load(name)
    else:
        requested = {
            c['name'] for c in containers_list
            if set(_tags(c)) & set(requested_tags)
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
    name_to_idx = {c['name']: i for i, c in enumerate(containers_list)}
    name_to_obj = {c['name']: c for c in containers_list}
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
    name_to_obj = {c['name']: c for c in containers_list}
    requested = {
        c['name'] for c in containers_list
        if set(_tags(c)) & set(requested_tags)
    }
    all_deps = set()
    frontier = list(requested)
    while frontier:
        for dep in deps_map.get(frontier.pop(), []):
            if dep in name_to_obj and dep not in all_deps and dep not in requested:
                all_deps.add(dep)
                frontier.append(dep)
    return [c for c in containers_list if c['name'] in all_deps]


def expand_with_deps(containers_list, deps_map, requested_tags, running_names):
    """Expand a tagged deployment to include unmet dependencies.

    For each container whose tags match requested_tags, walks the dep graph
    transitively and includes upstream roles that are not already running.
    The originally-requested containers are always included regardless of
    running state. Returns the effective subset in topological order.
    """
    name_to_obj = {c['name']: c for c in containers_list}
    running = set(running_names)

    requested = {
        c['name'] for c in containers_list
        if set(_tags(c)) & set(requested_tags)
    }

    all_needed = set(requested)
    frontier = list(requested)
    while frontier:
        for dep in deps_map.get(frontier.pop(), []):
            if dep in name_to_obj and dep not in all_needed:
                all_needed.add(dep)
                frontier.append(dep)

    effective = {n for n in all_needed if n in requested or n not in running}
    return [c for c in containers_list if c['name'] in effective]


class FilterModule:
    def filters(self):
        return {
            'build_dep_map': build_dep_map,
            'toposort_containers': toposort_containers,
            'dep_closure': dep_closure,
            'expand_with_deps': expand_with_deps,
        }
