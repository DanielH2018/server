"""The generated `dependsOn` of the bundle's Kustomizations (D4.2).

Every edge is generated, never hand-written, from an `Ordering` the caller reads out of
`ansible/filter_plugins/toposort.py` (`cluster.bundle_ordering`):

- a tag depends on the flux-owned members of its `build_k8s_dep_map` edges. An edge to any
  other role is dropped, because it would name a Kustomization that does not exist, and the
  annotation `homelab/ansible-owned-deps` lists it for a reader;
- a `K8S_APPLIES_LAST` tag also depends on every other tag's `<tag>-deferred`;
- `<tag>-deferred` depends on `<tag>`, and on the role that creates a namespace one of its
  documents names, by the same flux-owned rule;
- `<tag>-snapshot` inherits `<tag>`'s edges (C3), and `<tag>-verify` depends on the tag's last
  Kustomization: `<tag>-deferred` when the tag renders one, `<tag>` otherwise (D5.1a1).

`bundle.py` adds the `cluster-vars` edge and fails the render on a cycle, because Flux would
hold every Kustomization on it at `DependencyNotReady`. Like `bundle.py`, this module imports
neither Ansible nor Jinja. The plan is docs/target-architecture/deploy/d4-ordering.md, *The
renderer writes `dependsOn`*.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from lib import yaml_fast

# The suffix of a tag's deferred apply: its directory under `cluster/` and its Kustomization.
# A deploy tag may not end in it (`bundle.reserved_tags`).
DEFERRED_SUFFIX = "-deferred"
# The suffixes of D5's per-tag kinds: a role's snapshot Job and its verify or post-apply
# mutation Jobs, each in its own directory and Kustomization (C3, D5.1a1).
SNAPSHOT_SUFFIX = "-snapshot"
VERIFY_SUFFIX = "-verify"
# Every per-tag kind's suffix, in the order its document follows `<tag>`'s in
# `flux/<tag>.yaml`. The publish census expects exactly this order.
TAG_KIND_SUFFIXES = (DEFERRED_SUFFIX, SNAPSHOT_SUFFIX, VERIFY_SUFFIX)
# Lists, comma-separated and sorted, the edges a Kustomization's `dependsOn` leaves out because
# their role is Ansible-owned. Each becomes a real `dependsOn` in the first render after that
# role moves to Flux (D4.2).
ANSIBLE_OWNED_DEPS = "homelab/ansible-owned-deps"


@dataclass(frozen=True)
class Edges:
    """One Kustomization's generated edges, before the `cluster-vars` one.

    Attributes:
        kept: The names its `dependsOn` lists.
        dropped: The Ansible-owned roles it waits on in the play, which `dependsOn` cannot
            name. `ANSIBLE_OWNED_DEPS` lists them.
    """

    kept: frozenset[str] = frozenset()
    dropped: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Ordering:
    """What the bundle reads to write `dependsOn`, read by a caller that may import Ansible.

    Attributes:
        deps: Each deploy tag mapped to the names `build_k8s_dep_map` gives it, built from the
            full k8s `containers_list`.
        applies_last: The `K8S_APPLIES_LAST` tags.
        namespace_roles: Each namespace a role creates mapped to that role.
        flux_owned: The tags the inventory marks `owner: flux`, or None for the published
            tags. An edge to any other name is dropped and annotated.
    """

    deps: Mapping[str, Iterable[str]] = field(default_factory=dict)
    applies_last: frozenset[str] = frozenset()
    namespace_roles: Mapping[str, str] = field(default_factory=dict)
    flux_owned: frozenset[str] | None = None


NO_EDGES = Edges()
NO_ORDERING = Ordering()


def split_kind(name: str) -> tuple[str, str]:
    """The tag a Kustomization or directory name belongs to, and its per-tag suffix.

    The suffix is '' for the tag's own name. A deploy tag never ends in a suffix
    (`bundle.reserved_tags`), so the split is unambiguous.
    """
    for suffix in TAG_KIND_SUFFIXES:
        if name.endswith(suffix):
            return name.removesuffix(suffix), suffix
    return name, ""


def _namespaces(files: Mapping[str, str]) -> set[str]:
    """Each `metadata.namespace` a document in `files` names.

    A `kustomization.yaml` names none, since its `namespace` is a top-level field.
    """
    found = set()
    for text in files.values():
        for doc in yaml_fast.safe_load_all(text):
            if isinstance(doc, dict):
                namespace = (doc.get("metadata") or {}).get("namespace")
                if namespace:
                    found.add(str(namespace))
    return found


def kustomization_edges(
    tags: Iterable[str],
    directories: Mapping[str, Mapping[str, str]],
    ordering: Ordering,
) -> dict[str, Edges]:
    """Each Kustomization the published `tags` own mapped to its generated edges (D4.2).

    Args:
        tags: The published tags.
        directories: Each rendered directory's name under `cluster/` mapped to its files.
        ordering: The dependency map and the rules it feeds.

    Returns:
        `<tag>` for each tag, and `<tag><suffix>` for each per-tag kind whose directory the
        tag renders.
    """
    tags = sorted(set(tags))
    owned = set(tags) if ordering.flux_owned is None else set(ordering.flux_owned)
    deferring = [tag for tag in tags if tag + DEFERRED_SUFFIX in directories]
    edges = {}
    for tag in tags:
        wanted = set(ordering.deps.get(tag, ())) - {tag}
        kept = wanted & owned
        if tag in ordering.applies_last:
            # Every other tag's deferred apply, never its own: `<tag>-deferred` depends on
            # `<tag>`, so that edge would be a cycle.
            # DECIDED: an applies-last tag waits on no other tag's `-snapshot` or `-verify`.
            # D4 left the choice to D5.1a1. A verify Job probes through netpol-baseline's
            # fences, so netpol-baseline waiting on another tag's `-verify` would run that Job
            # before the fences it dials are applied. A snapshot gates only its own tag.
            kept |= {other + DEFERRED_SUFFIX for other in deferring if other != tag}
        edges[tag] = Edges(frozenset(kept), frozenset(wanted - owned))
    for tag in deferring:
        name = tag + DEFERRED_SUFFIX
        wanted = {
            ordering.namespace_roles[ns]
            for ns in _namespaces(directories[name])
            if ns in ordering.namespace_roles
        } - {tag}
        edges[name] = Edges(
            frozenset({tag} | (wanted & owned)), frozenset(wanted - owned)
        )
    for tag in tags:
        if tag + SNAPSHOT_SUFFIX in directories:
            edges[tag + SNAPSHOT_SUFFIX] = edges[tag]
        if tag + VERIFY_SUFFIX in directories:
            last = tag + DEFERRED_SUFFIX if tag + DEFERRED_SUFFIX in edges else tag
            edges[tag + VERIFY_SUFFIX] = Edges(frozenset({last}))
    return edges


def dependency_cycle(graph: Mapping[str, Iterable[str]]) -> list[str]:
    """A cycle in `graph`, each name mapped to the names it depends on, or [] when acyclic.

    The cycle is returned as a path that starts and ends on the same name. A name `graph`
    does not map has no edges.
    """
    state: dict[str, int] = {}  # 1 while on the walk's path, 2 once finished
    for root in sorted(graph):
        if state.get(root):
            continue
        path = [root]
        stack = [iter(sorted(graph.get(root, ())))]
        state[root] = 1
        while stack:
            dep = next(stack[-1], None)
            if dep is None:
                state[path.pop()] = 2
                stack.pop()
            elif state.get(dep) == 1:
                return path[path.index(dep) :] + [dep]
            elif not state.get(dep):
                state[dep] = 1
                path.append(dep)
                stack.append(iter(sorted(graph.get(dep, ()))))
    return []
