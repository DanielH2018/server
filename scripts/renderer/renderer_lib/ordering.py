"""The generated `dependsOn` of the bundle's Kustomizations (D4.2).

Every edge is generated, never hand-written, from an `Ordering` the caller reads out of
`scripts/renderer/template_filters/toposort.py` (`cluster.bundle_ordering`):

- a tag depends on its `build_k8s_dep_map` edges. An edge to a name that is no k8s tag fails
  the render, because it would name a Kustomization that does not exist;
- a `K8S_APPLIES_LAST` tag also depends on every other tag's `<tag>-deferred`;
- a tag with a document in `k8s_namespace`, in any of its directories, depends on the shared
  kind `homelab-namespace`, which creates that Namespace and its LimitRange (D5 step 30);
- `<tag>-deferred` depends on `<tag>`, and on the role that creates a namespace one of its
  documents names;
- `<tag>-snapshot` inherits `<tag>`'s edges (C3), and `<tag>` then depends on it, so the role
  applies only after its snapshot is taken (D5.2a). `<tag>-prepull` does the same, so the role
  applies only after its images are on the node. The two are siblings, never a chain,
  so the longer of them, not their sum, sits on the path. `<tag>-precheck`, the role's own
  pre-apply checks, does the same, so a failing check holds the role. It also depends
  on the role that creates a namespace one of its documents names, as `<tag>-deferred` does.
  `<tag>-verify` depends on the tag's last Kustomization: `<tag>-deferred` when the tag
  renders one, `<tag>` otherwise (D5.1a1). It also depends on the last Kustomization of each
  tag its role names in `flux_verify_after`, so its Jobs probe
  that tag's rolled workload rather than the one it replaces;
- an edge to a Kustomization that a suspended tag owns is dropped, and the annotation `homelab/suspended-deps` lists it, because Flux would hold the dependent at
  `DependencyNotReady` until the resume (C3 *Suspension*, D5.1b). `drop_suspended` is the one step.
  D5.3b2's `soak` keeps no edge at all (D5 Open question 19), and `soak.py` routes every tag's
  Kustomizations through this step so its annotation names the suspended ones.

`substitution` decides which Kustomizations substitute, and so which take the `cluster-vars`
edge. `bundle.py` adds that edge and fails the render on a cycle, because Flux would
hold every Kustomization on it at `DependencyNotReady`. Beside the per-tag kinds' suffixes,
`verify_files` reads which roles call for `<tag>-verify` and `precheck_files` which call for
`<tag>-precheck`. A role's `flux_cronjob_gates`, `flux_verify_after`, `flux_prepull_timeout`
and `flux_prepull_workloads` are `lib.k8s_roles.RoleDefaults` fields. Like `bundle.py`, this module imports
neither Ansible nor Jinja. The plan is docs/archive/deploy/d4-ordering.md, *The
renderer writes `dependsOn`*.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from pathlib import Path


from lib.k8s_roles import WHOLE_DURATION
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib.documents import parse_documents
from renderer.renderer_lib.namespace import HOMELAB_NAMESPACE

# The suffix of a tag's deferred apply: its directory under `cluster/` and its Kustomization.
# A deploy tag may not end in it (`bundle.reserved_tags`).
DEFERRED_SUFFIX = "-deferred"
SNAPSHOT_SUFFIX = "-snapshot"
VERIFY_SUFFIX = "-verify"
PREPULL_SUFFIX = "-prepull"
PRECHECK_SUFFIX = "-precheck"
# The order each kind's document follows `<tag>`'s in `flux/<tag>.yaml`; the publish census expects
# exactly it.
TAG_KIND_SUFFIXES = (
    DEFERRED_SUFFIX,
    SNAPSHOT_SUFFIX,
    PREPULL_SUFFIX,
    PRECHECK_SUFFIX,
    VERIFY_SUFFIX,
)
# So a hung Job fails on its own deadline before Flux reports a bare timeout.
VERIFY_DEADLINE_MARGIN_S = 60
# Verify templates live in `templates/verify/`; the manifests role derives its file lists from
# top-level `templates/*.yaml.j2` alone, so Ansible never renders them.
VERIFY_TEMPLATES_DIR = "verify"
# Precheck Jobs render into `cluster/<tag>-precheck/`, which `<tag>` depends on, so a failing check
# holds the role at its previous revision; Ansible never renders them.
PRECHECK_TEMPLATES_DIR = "precheck"


# Comma-separated, sorted edges a `dependsOn` leaves out because the tag's role sets
# `flux_suspend: true`; each returns on the first render after resume.
SUSPENDED_DEPS = "homelab/suspended-deps"


@dataclass(frozen=True)
class Edges:
    """One Kustomization's generated edges, before the `cluster-vars` one.

    Attributes:
        kept: The names its `dependsOn` lists.
        suspended: The Kustomizations of suspended tags it would otherwise depend on.
            `SUSPENDED_DEPS` lists them.
    """

    kept: frozenset[str] = frozenset()
    suspended: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Ordering:
    """What the bundle reads to write `dependsOn`, read by a caller that may import Ansible.

    Attributes:
        deps: Each deploy tag mapped to the names `build_k8s_dep_map` gives it, built from the
            full k8s `containers_list`. Its keys are every k8s tag, so an edge may name a
            tag outside the render, and an edge to any other name fails the render.
        applies_last: The `K8S_APPLIES_LAST` tags.
        namespace_roles: Each namespace a role creates mapped to that role.
        suspended: The tags whose role sets `flux_suspend: true`, read over every k8s
            role so a `--role` subset drops the same edges. An edge to any of their
            Kustomizations is dropped and annotated (`drop_suspended`).
        workload_namespace: `k8s_namespace`, whose Namespace the shared kind
            `homelab-namespace` creates. A tag with a document in it depends on that kind.
            None generates no such edge.
        verify_after: Each tag mapped to the other tags its `<tag>-verify` waits on, from
            its role's `flux_verify_after` (`RoleDefaults.verify_after`).
    """

    deps: Mapping[str, Iterable[str]] = field(default_factory=dict)
    applies_last: frozenset[str] = frozenset()
    namespace_roles: Mapping[str, str] = field(default_factory=dict)
    suspended: frozenset[str] = frozenset()
    workload_namespace: str | None = None
    verify_after: Mapping[str, Iterable[str]] = field(default_factory=dict)


NO_EDGES = Edges()
NO_ORDERING = Ordering()


def verify_files(
    tags: Iterable[str], roles: Path = K8S_ROLES, directory: str = VERIFY_TEMPLATES_DIR
) -> dict[str, list[str]]:
    """The verify manifests each tag's role ships, by basename, empty for a role that ships none.

    A role ships them as `templates/verify/<name>.j2`, and a non-empty list calls for
    `<tag>-verify`. The renderer (`renderer_lib/cluster.py`) and the publish census
    (`deploy_tools/bundle_census.py`) read this one function, so the directory the renderer
    writes and the Kustomization the census allows cannot disagree. `RoleDefaults.cronjob_gates` is the
    other input that calls for `<tag>-verify`. `directory` names another role-owned
    directory of `templates/` read the same way (`precheck_files`).
    """
    return {
        tag: sorted(
            p.name.removesuffix(".j2")
            for p in (roles / tag / "templates" / directory).glob("*.yaml.j2")
        )
        for tag in sorted(set(tags))
    }


def precheck_files(
    tags: Iterable[str], roles: Path = K8S_ROLES
) -> dict[str, list[str]]:
    """The pre-apply check manifests each tag's role ships, as `verify_files` reads verify ones.

    A role ships them as `templates/precheck/<name>.j2`, and a non-empty list calls for
    `<tag>-precheck`. Nothing else does.
    """
    return verify_files(tags, roles, PRECHECK_TEMPLATES_DIR)


def verify_timeout(files: Mapping[str, str], default: str) -> str:
    """`<tag>-verify`'s or `<tag>-precheck`'s Kustomization `timeout`, given its files.

    It is `default` unless a Job there sets an `activeDeadlineSeconds` within
    `VERIFY_DEADLINE_MARGIN_S` of it, and then that deadline plus the margin. A gate Job keeps
    its CronJob's deadline (`cronjob_gate.py`), which can sit at the bundle's default.

    Raises:
        ValueError: `default` is not a Go duration of whole hours, minutes and seconds.
    """
    deadlines = [
        int(obj.spec["activeDeadlineSeconds"])
        for obj, _ in parse_documents(files.values())
        if obj.kind == "Job" and obj.spec.get("activeDeadlineSeconds") is not None
    ]
    if not deadlines:
        return default
    floor = max(deadlines) + VERIFY_DEADLINE_MARGIN_S
    match = WHOLE_DURATION.fullmatch(default)
    if not default or match is None:
        raise ValueError(f"{default!r} is not a duration of h, m and s")
    hours, minutes, secs = (int(g or 0) for g in match.groups())
    return default if hours * 3600 + minutes * 60 + secs >= floor else f"{floor}s"


def split_kind(name: str) -> tuple[str, str]:
    """The tag a Kustomization or directory name belongs to, and its per-tag suffix.

    The suffix is '' for the tag's own name. A deploy tag never ends in a suffix
    (`bundle.reserved_tags`), so the split is unambiguous.
    """
    for suffix in TAG_KIND_SUFFIXES:
        if name.endswith(suffix):
            return name.removesuffix(suffix), suffix
    return name, ""


def substitution(
    tags: Iterable[str],
    directories: Mapping[str, object],
    key_sets: Mapping[str, frozenset[str]],
) -> tuple[dict[str, bool], set[str]]:
    """Which Kustomizations substitute, and which tags count as substituting (D3.3b).

    Args:
        tags: The published tags.
        directories: Each rendered directory's name under `cluster/`.
        key_sets: Each directory name mapped to its placeholder key set.

    Returns:
        Each directory name in `directories` that a tag in `tags` owns, `<tag>` and each
        `<tag><suffix>` alike, mapped to whether its own key set is non-empty. That alone
        decides `postBuild` and the `cluster-vars` edge on its Kustomization (C1
        *Substitution and escaping*). Second, the tags with any of their directories
        substituting, the per-tag set the `cluster-vars` footprint rule reads.
    """
    per_directory = {
        name: bool(key_sets.get(name))
        for tag in tags
        for name in (tag, *(tag + suffix for suffix in TAG_KIND_SUFFIXES))
        if name in directories
    }
    substituting = {
        split_kind(name)[0]
        for name, substitutes in per_directory.items()
        if substitutes
    }
    return per_directory, substituting


def _namespaces(files: Mapping[str, str]) -> set[str]:
    """Each `metadata.namespace` a document in `files` names.

    A `kustomization.yaml` names none, since its `namespace` is a top-level field.
    """
    return {
        obj.namespace for obj, _ in parse_documents(files.values()) if obj.namespace
    }


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

    Raises:
        ValueError: An edge names neither a published tag nor a key of `ordering.deps`.
    """
    tags = sorted(set(tags))
    known = set(tags) | set(ordering.deps)
    unknown: dict[str, set[str]] = {}

    def existing(name: str, wanted: set[str]) -> set[str]:
        if wanted - known:
            unknown[name] = wanted - known
        return wanted & known

    deferring = [tag for tag in tags if tag + DEFERRED_SUFFIX in directories]
    edges = {}
    for tag in tags:
        kept = existing(tag, set(ordering.deps.get(tag, ())) - {tag})
        if tag in ordering.applies_last:
            # Every other tag's deferred apply, never its own: `<tag>-deferred` depends on
            # `<tag>`, so that edge would be a cycle.
            # DECIDED: an applies-last tag waits on no other tag's `-snapshot` or `-verify`.
            # D4 left the choice to D5.1a1. A verify Job probes through netpol-baseline's
            # fences, so netpol-baseline waiting on another tag's `-verify` would run that Job
            # before the fences it dials are applied. A snapshot gates only its own tag.
            kept |= {other + DEFERRED_SUFFIX for other in deferring if other != tag}
        if ordering.workload_namespace and ordering.workload_namespace in {
            ns
            for name in (tag, *(tag + suffix for suffix in TAG_KIND_SUFFIXES))
            if name in directories
            for ns in _namespaces(directories[name])
        }:
            # Every bundle renders the shared kind, so the edge always names an existing
            # Kustomization.
            kept.add(HOMELAB_NAMESPACE)
        edges[tag] = Edges(frozenset(kept))
    for tag in deferring:
        name = tag + DEFERRED_SUFFIX
        wanted = {
            ordering.namespace_roles[ns]
            for ns in _namespaces(directories[name])
            if ns in ordering.namespace_roles
        } - {tag}
        edges[name] = Edges(frozenset({tag} | existing(name, wanted)))
    for tag in tags:
        # Each predecessor copies `<tag>`'s edges before `<tag>` gains any, so neither depends on
        # itself or the other.
        before = [
            tag + suffix
            for suffix in (SNAPSHOT_SUFFIX, PREPULL_SUFFIX, PRECHECK_SUFFIX)
            if tag + suffix in directories
        ]
        for name in before:
            edges[name] = edges[tag]
        if tag + PRECHECK_SUFFIX in directories:
            # A role's own checks apply before the role, so a namespace they name must exist
            # first. DECIDED: the tag itself is not subtracted, as `-deferred` subtracts it. A
            # precheck in a namespace its own tag creates then forms the cycle
            # `<tag>-precheck -> <tag>`, and `bundle_files` refuses the render, where dropping
            # the edge would leave the Job failing on a fresh cluster with no namespace.
            name = tag + PRECHECK_SUFFIX
            wanted = {
                ordering.namespace_roles[ns]
                for ns in _namespaces(directories[name])
                if ns in ordering.namespace_roles
            }
            edges[name] = Edges(edges[name].kept | existing(name, wanted))
        edges[tag] = Edges(edges[tag].kept | set(before))
        if tag + VERIFY_SUFFIX in directories:
            # A tag outside the render is kept by its own name, as a tag's edge onto it is.
            name = tag + VERIFY_SUFFIX
            wanted = set(ordering.verify_after.get(tag, ())) - {tag}
            after = {
                other + DEFERRED_SUFFIX
                if other + DEFERRED_SUFFIX in directories
                else other
                for other in existing(name, wanted)
            }
            last = tag + DEFERRED_SUFFIX if tag + DEFERRED_SUFFIX in edges else tag
            edges[name] = Edges(frozenset({last} | after))
    if unknown:
        raise ValueError(
            "a dependsOn edge names no k8s tag, so no Kustomization of that name exists: "
            + "; ".join(
                f"{name} -> {', '.join(sorted(names))}"
                for name, names in sorted(unknown.items())
            )
        )
    return drop_suspended(edges, ordering.suspended)


def drop_suspended(
    edges: Mapping[str, Edges], suspended: Iterable[str]
) -> dict[str, Edges]:
    """`edges` with every edge to a Kustomization of a `suspended` tag moved to `suspended`.

    A suspended Kustomization never advances its revision, so a dependent would wait on it
    until the resume (D5.1b). A Kustomization of a suspended tag is `<tag>` or `<tag>` with a
    per-tag suffix (`split_kind`).

    Args:
        edges: Each Kustomization mapped to its generated edges.
        suspended: The tags whose role sets `flux_suspend: true`.
    """
    suspended = frozenset(suspended)

    def is_suspended(name: str) -> bool:
        return split_kind(name)[0] in suspended

    result = {}
    for name, edge in edges.items():
        gone = frozenset(n for n in edge.kept if is_suspended(n))
        result[name] = Edges(edge.kept - gone, edge.suspended | gone)
    return result


def dependency_cycle(graph: Mapping[str, Iterable[str]]) -> list[str]:
    """A cycle in `graph`, each name mapped to the names it depends on, or [] when acyclic.

    The cycle is returned as a path that starts and ends on the same name. A name `graph`
    does not map has no edges.
    """
    sorter = TopologicalSorter(
        {name: sorted(deps) for name, deps in sorted(graph.items())}
    )
    try:
        sorter.prepare()
    except CycleError as error:
        return error.args[1][::-1]
    return []
