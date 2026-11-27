"""The two promotion footprint readers agree over generated `digests.json` files.

`forward_only.changed_tags` decides whether a failed revision resets `prod`, and
`verify_promotion.footprints_match` decides whether a held candidate may pass. Both read the
same bundles, so a tag the first calls unchanged must match under the second. The seed and the
example count come from `tests/hypothesis_profiles.py`.

Run: uv run pytest scripts/deploy_tools/tests/promote/test_promote_footprint_properties.py
"""

import json
from typing import NamedTuple

from hypothesis import given
from hypothesis import strategies as st

from deploy_tools.promote_lib import forward_only, verify_promotion

UNCLASSIFIED = forward_only.UNCLASSIFIED
_TAGS = ("longhorn", "metallb", "bento-pdf", "soak")
_HASHES = st.sampled_from(["h1", "h2", "h3"])


class Side(NamedTuple):
    """One bundle, and its footprints as built, or None when it was built unreadable."""

    files: dict[str, bytes] | None
    prints: dict[str, str] | None

    @property
    def parsed(self) -> object | None:
        return verify_promotion.bundle_digests(self.files)


def _bundle(digests: object) -> dict[str, bytes]:
    return {verify_promotion.DIGESTS_FILE: json.dumps(digests).encode()}


def _entry(footprint: str) -> dict:
    return {"own": footprint, "footprint": footprint, "claims": [], "suspended": False}


_PRINTS = st.dictionaries(st.sampled_from((*_TAGS, UNCLASSIFIED)), _HASHES)
_READABLE = _PRINTS.map(
    lambda prints: Side(_bundle({n: _entry(f) for n, f in prints.items()}), prints)
)
_BAD_ENTRIES = st.sampled_from(["h1", None, {}, {"own": "h1"}, {"footprint": 1}])
_UNREADABLE = st.one_of(
    st.sampled_from(
        [
            None,
            {},
            {verify_promotion.DIGESTS_FILE: b"{not json"},
            _bundle([_entry("h1")]),
        ]
    ),
    # One bad entry spoils a file whose other entries are sound.
    st.builds(
        lambda prints, name, bad: _bundle(
            {**{n: _entry(f) for n, f in prints.items()}, name: bad}
        ),
        _PRINTS,
        st.sampled_from((*_TAGS, UNCLASSIFIED)),
        _BAD_ENTRIES,
    ),
).map(lambda files: Side(files, None))
_SIDES = st.one_of(_READABLE, _UNREADABLE)


@given(_SIDES)
def test_a_file_against_itself_changes_no_tag(side):
    expected = None if side.prints is None else frozenset()
    assert forward_only.changed_tags(side.parsed, side.parsed) == expected


@given(_SIDES, _SIDES)
def test_changed_tags_is_symmetric_and_unknown_only_for_an_unreadable_side(a, b):
    changed = forward_only.changed_tags(a.parsed, b.parsed)
    assert changed == forward_only.changed_tags(b.parsed, a.parsed)
    assert (changed is None) == (a.prints is None or b.prints is None)


@given(_PRINTS, _PRINTS, st.lists(_HASHES, min_size=2, max_size=2, unique=True))
def test_a_differing_unclassified_changes_every_other_name(ours, theirs, pair):
    ours = {**ours, UNCLASSIFIED: pair[0]}
    theirs = {**theirs, UNCLASSIFIED: pair[1]}
    a, b = ({n: _entry(f) for n, f in prints.items()} for prints in (ours, theirs))
    assert forward_only.changed_tags(a, b) == (ours.keys() | theirs.keys()) - {
        UNCLASSIFIED
    }


@given(_SIDES, _SIDES, st.data())
def test_names_changed_tags_leaves_out_match_footprints(a, b, data):
    changed = forward_only.changed_tags(a.parsed, b.parsed)
    if changed is None:
        return
    # `names` come from a `failed=` hold, which names Kustomizations, never UNCLASSIFIED.
    unchanged = [n for n in _TAGS if n not in changed]
    names = data.draw(
        st.frozensets(st.sampled_from(unchanged)) if unchanged else st.just(frozenset())
    )
    assert verify_promotion.footprints_match(a.files, b.files, names)
