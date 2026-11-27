"""`digests` routes every bundle path to a footprint, and `own` moves with its tag's files alone.

Paths come from small tag and kind pools, so suffix directories, `flux/<x>.yaml`, the index
paths and a shared kind with no footprint rule all occur. A `flux/` file holds YAML, because
`digests` reads `suspended` from it. The property tests run under the profile
`tests/hypothesis_profiles.py` loads.

Run: uv run pytest scripts/renderer/tests/bundle/test_renderer_digest_properties.py
"""

from hypothesis import given
from hypothesis import strategies as st

from renderer.renderer_lib.bundle import (
    CLUSTER_DIR,
    CLUSTER_VARS,
    DIGESTS,
    FLUX_DIR,
    FLUX_INDEX,
    HOMELAB_NAMESPACE,
    SHARED_KINDS,
    UNCLASSIFIED,
    digests,
    own_digests,
)
from renderer.renderer_lib.ordering import TAG_KIND_SUFFIXES

_TAGS = ("a", "b", "c")
_KINDS = (*sorted(SHARED_KINDS), "no-rule")
_NAMES = (*_TAGS, *_KINDS, "stray")
_FLUX_TEXTS = ("", "a: 1\n", "spec:\n  suspend: true\n")
# D6's *What a footprint counts*, restated so a changed `FOOTPRINT_RULES` entry goes red.
_KIND_TAGS = {
    CLUSTER_VARS: lambda tags, substituting: tags & substituting,
    "soak": lambda tags, substituting: frozenset(),
    HOMELAB_NAMESPACE: lambda tags, substituting: tags,
}

_paths = st.one_of(
    st.builds(
        lambda name, suffix, leaf: f"{CLUSTER_DIR}/{name}{suffix}/{leaf}",
        st.sampled_from(_NAMES),
        st.sampled_from(("", *TAG_KIND_SUFFIXES)),
        st.sampled_from(("f", "sub/g")),
    ),
    st.builds(lambda name: f"{FLUX_DIR}/{name}.yaml", st.sampled_from(_NAMES)),
    st.sampled_from(
        ("images/x", "odd/x", f"{CLUSTER_DIR}/x", f"{FLUX_DIR}/x.json")
        + (FLUX_INDEX, DIGESTS)
    ),
)


def _content(path: str) -> st.SearchStrategy[bytes]:
    texts = (
        st.sampled_from(_FLUX_TEXTS)
        if path.startswith(f"{FLUX_DIR}/")
        else st.text(max_size=8)
    )
    return texts.map(str.encode)


_files = st.lists(_paths, unique=True).flatmap(
    lambda paths: st.fixed_dictionaries({p: _content(p) for p in paths})
)


@st.composite
def _bundles(draw) -> tuple[dict[str, bytes], frozenset, frozenset, frozenset]:
    tags = draw(st.frozensets(st.sampled_from(_TAGS)))
    substituting = draw(st.frozensets(st.sampled_from(_TAGS)))
    kinds = draw(st.frozensets(st.sampled_from(_KINDS)))
    return draw(_files), tags, substituting, kinds


def _excluded(path: str) -> bool:
    return path in (FLUX_INDEX, DIGESTS) or path.startswith("images/")


def _owner(path: str) -> str | None:
    """The name whose `cluster/<name><suffix>/` or `flux/<name>.yaml` holds `path`."""
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == CLUSTER_DIR:
        suffix = next((s for s in TAG_KIND_SUFFIXES if parts[1].endswith(s)), "")
        return parts[1].removesuffix(suffix)
    if len(parts) == 2 and parts[0] == FLUX_DIR and parts[1].endswith(".yaml"):
        return parts[1].removesuffix(".yaml")
    return None


def _footprints(
    path: str, tags: frozenset, substituting: frozenset, kinds: frozenset
) -> set[str]:
    """The entries whose footprint a change to the non-excluded `path` must move."""
    name = _owner(path)
    if name is None or name not in tags | kinds:
        return {UNCLASSIFIED}
    if name not in kinds:
        return {name}
    if name not in _KIND_TAGS:
        return {name, UNCLASSIFIED}
    return {name} | _KIND_TAGS[name](tags, substituting)


def _moved(before: dict, after: dict, field: str) -> set[str]:
    """The entries whose `field` digest differs between two `digests` results."""
    return {name for name in before if before[name][field] != after[name][field]}


@given(bundle=_bundles(), data=st.data())
def test_one_path_changing_moves_own_for_its_directory_and_the_footprints_it_counts_for(
    bundle, data
):
    files, tags, substituting, kinds = bundle
    path = data.draw(_paths, label="path")
    replacement = data.draw(st.none() | _content(path), label="replacement")
    mutated = dict(files)
    if replacement is None:
        mutated.pop(path, None)
    else:
        mutated[path] = replacement
    before = digests(files, tags, substituting, kinds)
    after = digests(mutated, tags, substituting, kinds)
    changed = files.get(path) != mutated.get(path)

    owners = {
        name
        for name in tags | kinds
        if changed and path.startswith(f"{CLUSTER_DIR}/{name}/")
    }
    assert _moved(before, after, "own") - {UNCLASSIFIED} == owners
    assert _moved(before, after, "own") <= _moved(before, after, "footprint")
    expected = (
        set()
        if not changed or _excluded(path)
        else _footprints(path, tags, substituting, kinds)
    )
    assert _moved(before, after, "footprint") == expected


@given(bundle=_bundles())
def test_own_digests_matches_each_tags_own_in_digests(bundle):
    files, tags, substituting, kinds = bundle
    result = digests(files, tags, substituting, kinds)
    texts = {path: data.decode() for path, data in files.items()}
    assert own_digests(texts, tags) == {tag: result[tag]["own"] for tag in tags}


@given(bundle=_bundles(), data=st.data())
def test_digests_ignore_insertion_order(bundle, data):
    files, tags, substituting, kinds = bundle
    order = data.draw(st.permutations(list(files)), label="order")
    reordered = {path: files[path] for path in order}
    assert digests(reordered, tags, substituting, kinds) == digests(
        files, tags, substituting, kinds
    )
