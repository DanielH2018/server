"""The generated `dependsOn` of the bundle's Kustomizations (D4.2).

Every edge is generated, never hand-written, from an `Ordering` the caller reads out of
`ansible/filter_plugins/toposort.py` (`cluster.bundle_ordering`):

- a tag depends on the flux-owned members of its `build_k8s_dep_map` edges. An edge to any
  other role is dropped, because it would name a Kustomization that does not exist, and the
  annotation `homelab/ansible-owned-deps` lists it for a reader;
- a `K8S_APPLIES_LAST` tag also depends on every other tag's `<tag>-deferred`;
- a tag with a document in `k8s_namespace`, in any of its directories, depends on the shared
  kind `homelab-namespace`, which creates that Namespace and its LimitRange (D5 step 30);
- `<tag>-deferred` depends on `<tag>`, and on the role that creates a namespace one of its
  documents names, by the same flux-owned rule;
- `<tag>-snapshot` inherits `<tag>`'s edges (C3), and `<tag>` then depends on it, so the role
  applies only after its snapshot is taken (D5.2a). `<tag>-prepull` does the same, so the role
  applies only after its images are on the node (#5525). The two are siblings, never a chain,
  so the longer of them, not their sum, sits on the path. `<tag>-precheck`, the role's own
  pre-apply checks, does the same, so a failing check holds the role (#5719). It also depends
  on the role that creates a namespace one of its documents names, as `<tag>-deferred` does.
  `<tag>-verify` depends on the tag's last Kustomization: `<tag>-deferred` when the tag
  renders one, `<tag>` otherwise (D5.1a1). It also depends on the last Kustomization of each
  tag its role names in `flux_verify_after`, by the same flux-owned rule, so its Jobs probe
  that tag's rolled workload rather than the one it replaces (#5999);
- an edge to a Kustomization that a suspended tag owns is dropped, and the annotation `homelab/suspended-deps` lists it, because Flux would hold the dependent at
  `DependencyNotReady` until the resume (C3 *Suspension*, D5.1b). `drop_suspended` is the one step.
  D5.3b2's `soak` keeps no edge at all (D5 Open question 19), and `soak.py` routes every tag's
  Kustomizations through this step so its annotation names the suspended ones.

`substitution` decides which Kustomizations substitute, and so which take the `cluster-vars`
edge. `bundle.py` adds that edge and fails the render on a cycle, because Flux would
hold every Kustomization on it at `DependencyNotReady`. Beside the per-tag kinds' suffixes,
`verify_files` and `cronjob_gates` read which roles call for `<tag>-verify`, `verify_after`
which other tags it waits on,
`prepull_timeouts` which call for `<tag>-prepull`, and `precheck_files` which call for
`<tag>-precheck`. Like `bundle.py`, this module imports
neither Ansible nor Jinja. The plan is docs/target-architecture/deploy/d4-ordering.md, *The
renderer writes `dependsOn`*.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from lib import yaml_fast
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib.namespace import HOMELAB_NAMESPACE

# The suffix of a tag's deferred apply: its directory under `cluster/` and its Kustomization.
# A deploy tag may not end in it (`bundle.reserved_tags`).
DEFERRED_SUFFIX = "-deferred"
# The suffixes of D5's per-tag kinds: a role's snapshot Job and its verify or post-apply
# mutation Jobs, each in its own directory and Kustomization (C3, D5.1a1).
SNAPSHOT_SUFFIX = "-snapshot"
VERIFY_SUFFIX = "-verify"
# A role's pre-pull Job, which pulls the tag's images onto its node before `<tag>` applies
# (`prepull.py`, #5525).
PREPULL_SUFFIX = "-prepull"
# A role's own pre-apply check Jobs, such as pihole's sibling guard, which `<tag>` waits on
# (#5719).
PRECHECK_SUFFIX = "-precheck"
# Every per-tag kind's suffix, in the order its document follows `<tag>`'s in
# `flux/<tag>.yaml`. The publish census expects exactly this order.
TAG_KIND_SUFFIXES = (
    DEFERRED_SUFFIX,
    SNAPSHOT_SUFFIX,
    PREPULL_SUFFIX,
    PRECHECK_SUFFIX,
    VERIFY_SUFFIX,
)
# The role default that calls for `<tag>-prepull` and sets its Kustomization's `timeout`.
PREPULL_TIMEOUT = "flux_prepull_timeout"
_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")
# How far a `<tag>-verify` or `<tag>-precheck` timeout sits above its Jobs' longest `activeDeadlineSeconds`, so a
# hung Job fails on its own deadline before Flux reports a bare timeout.
VERIFY_DEADLINE_MARGIN_S = 60
# A role's verify and post-apply mutation Jobs, with whatever they need beside them, are its
# `templates/verify/*.yaml.j2`. They render into `cluster/<tag>-verify/` (D5.1a1, D5.1a3).
# The manifests role derives its file lists from the top-level `templates/*.yaml.j2` alone, so
# Ansible never renders or applies them.
VERIFY_TEMPLATES_DIR = "verify"
# A role's pre-apply check Jobs, with whatever they need beside them, are its
# `templates/precheck/*.yaml.j2`. They render into `cluster/<tag>-precheck/`, which `<tag>`
# depends on, so a check that fails holds the role at its previous revision (#5719). Ansible
# never renders them, as it never renders `templates/verify/`.
PRECHECK_TEMPLATES_DIR = "precheck"
# The role default that opts a role's CronJobs into gate Jobs in `<tag>-verify`, and the two
# classes a gate takes (`cronjob_gates`).
CRONJOB_GATES = "flux_cronjob_gates"
GATE_KINDS = frozenset({"verify", "mutation"})
# The role default that names the other tags whose rolled workload `<tag>-verify` probes
# (`verify_after`, #5999).
VERIFY_AFTER = "flux_verify_after"


# Lists, comma-separated and sorted, the edges a Kustomization's `dependsOn` leaves out because
# their role is Ansible-owned. Each becomes a real `dependsOn` in the first render after that
# role moves to Flux (D4.2).
ANSIBLE_OWNED_DEPS = "homelab/ansible-owned-deps"
# Lists, comma-separated and sorted, the edges a Kustomization's `dependsOn` leaves out because
# they name a Kustomization of a tag whose role sets `flux_suspend: true` (D5.1b). Each comes
# back in the first render after the resume.
SUSPENDED_DEPS = "homelab/suspended-deps"


@dataclass(frozen=True)
class Edges:
    """One Kustomization's generated edges, before the `cluster-vars` one.

    Attributes:
        kept: The names its `dependsOn` lists.
        dropped: The Ansible-owned roles it waits on in the play, which `dependsOn` cannot
            name. `ANSIBLE_OWNED_DEPS` lists them.
        suspended: The Kustomizations of suspended tags it would otherwise depend on.
            `SUSPENDED_DEPS` lists them.
    """

    kept: frozenset[str] = frozenset()
    dropped: frozenset[str] = frozenset()
    suspended: frozenset[str] = frozenset()


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
        suspended: The tags whose role sets `flux_suspend: true`, read over every flux-owned
            role so a `--role` subset drops the same edges. An edge to any of their
            Kustomizations is dropped and annotated (`drop_suspended`).
        workload_namespace: `k8s_namespace`, whose Namespace the shared kind
            `homelab-namespace` creates. A tag with a document in it depends on that kind.
            None generates no such edge.
        verify_after: Each tag mapped to the other tags its `<tag>-verify` waits on, from
            its role's `flux_verify_after` (`verify_after`).
    """

    deps: Mapping[str, Iterable[str]] = field(default_factory=dict)
    applies_last: frozenset[str] = frozenset()
    namespace_roles: Mapping[str, str] = field(default_factory=dict)
    flux_owned: frozenset[str] | None = None
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
    writes and the Kustomization the census allows cannot disagree. `cronjob_gates` is the
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
    `<tag>-precheck` (#5719). Nothing else does.
    """
    return verify_files(tags, roles, PRECHECK_TEMPLATES_DIR)


def prepull_timeouts(
    tags: Iterable[str], roles: Path = K8S_ROLES
) -> tuple[dict[str, str], list[str]]:
    """Each tag whose role sets `flux_prepull_timeout` mapped to it, and one line per problem.

    The value calls for `<tag>-prepull` and is that Kustomization's `timeout` (#5525). The
    renderer (`prepull.py`, `bundle.bundle_files`) and the publish census read this one
    function, as they read `verify_files`. A value must be a literal Go duration, as
    `role_fields` requires of `manifests_rollout_timeout`; any other value is a problem and
    its tag is left out.
    """
    found, problems = {}, []
    for tag in sorted(set(tags)):
        path = roles / tag / "defaults" / "main.yml"
        try:
            defaults = yaml_fast.safe_load(path.read_text()) if path.is_file() else {}
        except (OSError, yaml.YAMLError) as err:
            problems.append(f"could not read {tag}'s {PREPULL_TIMEOUT}: {err}")
            continue
        value = (defaults or {}).get(PREPULL_TIMEOUT)
        if value is None:
            continue
        if not (isinstance(value, str) and value and _DURATION.fullmatch(value)):
            problems.append(
                f"{tag}'s {PREPULL_TIMEOUT} is {value!r}, not a literal duration such as "
                "3600s, so its pre-pull Kustomization timeout cannot be read"
            )
            continue
        found[tag] = value
    return found, problems


def cronjob_gates(
    tags: Iterable[str], roles: Path = K8S_ROLES
) -> tuple[dict[str, dict[str, dict]], list[str]]:
    """Each tag whose role sets `flux_cronjob_gates` mapped to its gates, and one line per problem.

    The value maps a CronJob the role renders to its gate's class, R1's census row for it:
    `{kind: verify}`, or `{kind: mutation, rerun_safe: <path>::<function>}`. A gate calls for
    `<tag>-verify`, which holds the gate Job `cronjob_gate.py` copies from the CronJob (D5,
    *`cronjob-gate`'s gate Job*). The renderer and the publish census read this one function,
    as they read `verify_files`. A value must be a literal of that shape; any other value is a
    problem and its tag is left out.
    """
    found, problems = {}, []
    for tag in sorted(set(tags)):
        path = roles / tag / "defaults" / "main.yml"
        try:
            defaults = yaml_fast.safe_load(path.read_text()) if path.is_file() else {}
        except (OSError, yaml.YAMLError) as err:
            problems.append(f"could not read {tag}'s {CRONJOB_GATES}: {err}")
            continue
        value = (defaults or {}).get(CRONJOB_GATES)
        if value is None:
            continue
        wrong = _gate_problems(value)
        if wrong:
            problems += [f"{tag}'s {CRONJOB_GATES}: {line}" for line in wrong]
            continue
        found[tag] = {str(name): dict(gate) for name, gate in value.items()}
    return found, problems


def verify_after(
    tags: Iterable[str], roles: Path = K8S_ROLES
) -> tuple[dict[str, list[str]], list[str]]:
    """Each tag whose role sets `flux_verify_after` mapped to it, and one line per problem.

    The value lists the other tags whose rolled workload the role's verify Jobs probe, so
    `<tag>-verify` waits on each one's last Kustomization as well as on its own tag's
    (#5999). crowdsec's `crowdsec-verify` names `traefik`: its ban proof reads the edge's
    answer, and without the edge it can pass against the Traefik pod a `Recreate` roll is
    about to replace. A value must be a literal non-empty list of tag names; any other value
    is a problem and its tag is left out.
    """
    found, problems = {}, []
    for tag in sorted(set(tags)):
        path = roles / tag / "defaults" / "main.yml"
        try:
            defaults = yaml_fast.safe_load(path.read_text()) if path.is_file() else {}
        except (OSError, yaml.YAMLError) as err:
            problems.append(f"could not read {tag}'s {VERIFY_AFTER}: {err}")
            continue
        value = (defaults or {}).get(VERIFY_AFTER)
        if value is None:
            continue
        if not (
            isinstance(value, list)
            and value
            and all(isinstance(name, str) and name and name != tag for name in value)
        ):
            problems.append(
                f"{tag}'s {VERIFY_AFTER} is {value!r}, not a literal non-empty list of "
                "other tags' names"
            )
            continue
        found[tag] = sorted(set(value))
    return found, problems


def _gate_problems(value: object) -> list[str]:
    """Why `value` is not a literal `flux_cronjob_gates` mapping, empty when it is one."""
    if not isinstance(value, dict) or not value:
        return [
            f"{value!r} is not a non-empty mapping of CronJob names to gate classes"
        ]
    problems = []
    for name, gate in value.items():
        if not isinstance(gate, dict) or gate.get("kind") not in GATE_KINDS:
            problems.append(f"{name}: {gate!r} names no kind of {sorted(GATE_KINDS)}")
            continue
        extra = set(gate) - {"kind", "rerun_safe"}
        rerun = gate.get("rerun_safe")
        if extra:
            problems.append(f"{name}: unknown keys {sorted(extra)}")
        elif gate["kind"] == "mutation" and not (
            isinstance(rerun, str) and "::" in rerun
        ):
            problems.append(
                f"{name}: a mutation gate names its rerun_safe test as <path>::<function>"
            )
        elif gate["kind"] == "verify" and rerun is not None:
            problems.append(
                f"{name}: a verify gate never re-runs, so takes no rerun_safe"
            )
    return problems


def verify_timeout(files: Mapping[str, str], default: str) -> str:
    """`<tag>-verify`'s or `<tag>-precheck`'s Kustomization `timeout`, given its files.

    It is `default` unless a Job there sets an `activeDeadlineSeconds` within
    `VERIFY_DEADLINE_MARGIN_S` of it, and then that deadline plus the margin. A gate Job keeps
    its CronJob's deadline (`cronjob_gate.py`), which can sit at the bundle's default.

    Raises:
        ValueError: `default` is not a Go duration of whole hours, minutes and seconds.
    """
    deadlines = [
        int(doc["spec"]["activeDeadlineSeconds"])
        for text in files.values()
        for doc in yaml_fast.safe_load_all(text)
        if isinstance(doc, dict)
        and doc.get("kind") == "Job"
        and (doc.get("spec") or {}).get("activeDeadlineSeconds") is not None
    ]
    if not deadlines:
        return default
    floor = max(deadlines) + VERIFY_DEADLINE_MARGIN_S
    match = _DURATION.fullmatch(default)
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
        if ordering.workload_namespace and ordering.workload_namespace in {
            ns
            for name in (tag, *(tag + suffix for suffix in TAG_KIND_SUFFIXES))
            if name in directories
            for ns in _namespaces(directories[name])
        }:
            # Every bundle renders the shared kind, so the edge always names a Kustomization
            # that exists. The `-snapshot`, `-prepull` and `-precheck` copies below inherit it,
            # and the tag's other kinds wait on `<tag>`.
            kept.add(HOMELAB_NAMESPACE)
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
        # Each predecessor copies `<tag>`'s edges before `<tag>` gains any, so neither depends
        # on itself or on the other.
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
            edges[name] = Edges(
                edges[name].kept | (wanted & owned),
                edges[name].dropped | (wanted - owned),
            )
        edges[tag] = Edges(edges[tag].kept | set(before), edges[tag].dropped)
        if tag + VERIFY_SUFFIX in directories:
            # The other tags' last Kustomizations, read from `directories`: a flux-owned tag
            # outside the render is kept by its own name, as a tag's edge onto it is.
            wanted = set(ordering.verify_after.get(tag, ())) - {tag}
            after = {
                other + DEFERRED_SUFFIX
                if other + DEFERRED_SUFFIX in directories
                else other
                for other in wanted & owned
            }
            last = tag + DEFERRED_SUFFIX if tag + DEFERRED_SUFFIX in edges else tag
            edges[tag + VERIFY_SUFFIX] = Edges(
                frozenset({last} | after), frozenset(wanted - owned)
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
        result[name] = Edges(edge.kept - gone, edge.dropped, edge.suspended | gone)
    return result


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
