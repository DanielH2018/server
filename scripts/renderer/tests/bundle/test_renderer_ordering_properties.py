"""`kustomization_edges` builds an acyclic `dependsOn` graph with no dangling edge, whatever the tag order.

Each case draws published tags from a small pool, the per-tag kinds each renders, a dependency
DAG over the pool and the namespaces each document names. The draw keeps to the inputs the
upstream code produces, so a cycle the graph shows is one `kustomization_edges` made. The
property tests run under the profile `tests/hypothesis_profiles.py` loads.

Run: uv run pytest scripts/renderer/tests/bundle/test_renderer_ordering_properties.py
"""

from collections.abc import Iterable
from dataclasses import dataclass

from hypothesis import example, given
from hypothesis import strategies as st

from renderer.renderer_lib.bundle import SHARED_KINDS
from renderer.renderer_lib.ordering import (
    DEFERRED_SUFFIX,
    PRECHECK_SUFFIX,
    TAG_KIND_SUFFIXES,
    Edges,
    Ordering,
    dependency_cycle,
    kustomization_edges,
)

POOL = ("t0", "t1", "t2", "t3", "t4", "t5", "t6")
NAMESPACES = ("homelab", "media", "observability")


@dataclass(frozen=True)
class Case:
    """One `kustomization_edges` input, with the namespaces each directory's documents name."""

    tags: list[str]
    directories: dict[str, dict[str, str]]
    ordering: Ordering
    namespaces: dict[str, set[str]]

    def edges(self, tags: Iterable[str] | None = None) -> dict[str, Edges]:
        return kustomization_edges(
            self.tags if tags is None else tags, self.directories, self.ordering
        )


def _subset(items):
    return st.sets(st.sampled_from(sorted(items))) if items else st.just(set())


def _files(namespaces: list[str | None]) -> dict[str, str]:
    return {
        f"{n}.yaml": "kind: ConfigMap\nmetadata:\n  name: x\n"
        + (f"  namespace: {ns}\n" if ns else "")
        for n, ns in enumerate(namespaces)
    }


@st.composite
def cases(draw) -> Case:
    tags = sorted(draw(st.sets(st.sampled_from(POOL), min_size=1, max_size=6)))
    applies_last = draw(_subset(tags))
    # `toposort_containers` refuses an edge onto an applies-last tag, and deps run only to
    # lower-index tags, so the map is a DAG.
    deps = {
        name: draw(_subset(set(POOL[:i]) - applies_last)) for i, name in enumerate(POOL)
    }
    # An applies-last owner would close `<tag>-deferred -> owner -> <tag>-deferred`.
    owners = sorted(set(POOL) - applies_last)
    namespace_roles = draw(
        st.dictionaries(st.sampled_from(NAMESPACES), st.sampled_from(owners))
    )
    directories, namespaces = {}, {}
    for tag in tags:
        index = POOL.index(tag)
        # A precheck's namespace owner may not depend on its tag; its own tag is the
        # documented `<tag>-precheck -> <tag>` cycle.
        precheck_namespaces = [
            ns for ns in NAMESPACES if POOL.index(namespace_roles.get(ns, tag)) <= index
        ]
        for suffix in ("", *draw(st.sets(st.sampled_from(TAG_KIND_SUFFIXES)))):
            allowed = precheck_namespaces if suffix == PRECHECK_SUFFIX else NAMESPACES
            namespace = st.none() | st.sampled_from(allowed) if allowed else st.none()
            named = draw(st.lists(namespace, max_size=2))
            directories[tag + suffix] = _files(named)
            namespaces[tag + suffix] = {ns for ns in named if ns}
    ordering = Ordering(
        deps=deps,
        applies_last=frozenset(applies_last),
        namespace_roles=namespace_roles,
        suspended=frozenset(draw(_subset(tags))),
        workload_namespace=draw(st.none() | st.sampled_from(NAMESPACES)),
        verify_after={tag: draw(_subset(POOL)) for tag in tags},
    )
    return Case(tags, directories, ordering, namespaces)


def _deferring(*tags: str) -> Case:
    """Each of `tags` rendering `<tag>` and `<tag>-deferred`, all of them applies-last."""
    names = [name for tag in tags for name in (tag, tag + DEFERRED_SUFFIX)]
    return Case(
        list(tags),
        {name: _files(["homelab"]) for name in names},
        Ordering(deps={tag: [] for tag in tags}, applies_last=frozenset(tags)),
        {name: {"homelab"} for name in names},
    )


@given(case=cases())
def test_no_kustomization_depends_on_itself_or_both_keeps_and_suspends_an_edge(case):
    for name, edge in case.edges().items():
        assert name not in edge.kept | edge.suspended
        assert not edge.kept & edge.suspended


@given(case=cases())
def test_every_edge_names_a_rendered_kustomization_a_k8s_tag_or_a_shared_kind(case):
    edges = case.edges()
    known = set(edges) | set(case.ordering.deps) | set(SHARED_KINDS)

    for edge in edges.values():
        assert edge.kept | edge.suspended <= known


@given(case=cases())
@example(case=_deferring("a", "b"))
def test_the_graph_is_acyclic_but_for_a_precheck_in_its_own_tags_namespace(case):
    graph = {
        name: set(edge.kept | edge.suspended) for name, edge in case.edges().items()
    }
    for tag in case.tags:
        precheck = tag + PRECHECK_SUFFIX
        owners = {
            case.ordering.namespace_roles.get(ns)
            for ns in case.namespaces.get(precheck, ())
        }
        if tag in owners:
            graph[precheck].remove(tag)

    assert dependency_cycle(graph) == []


@given(case=cases())
@example(case=_deferring("a", "b"))
def test_an_applies_last_tag_depends_on_the_deferred_apply_of_each_tag_outside_applies_last(
    case,
):
    edges = case.edges()
    deferred = {
        tag + DEFERRED_SUFFIX
        for tag in case.tags
        if tag + DEFERRED_SUFFIX in case.directories
        and tag not in case.ordering.applies_last
    }

    for tag in case.ordering.applies_last:
        named = edges[tag].kept | edges[tag].suspended
        assert {n for n in named if n.endswith(DEFERRED_SUFFIX)} == deferred


@given(case=cases(), data=st.data())
def test_the_edges_do_not_depend_on_tag_order_or_repeats(case, data):
    shuffled = data.draw(st.permutations(case.tags))
    repeats = data.draw(st.lists(st.sampled_from(case.tags), max_size=3))

    assert case.edges(shuffled + repeats) == case.edges()
