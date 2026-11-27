"""Passes 2 and 3 survive Flux's envsubst: generated text comes back with each sentinel's value.

A text is a run of literal chunks and sentinels. The literal alphabet has no capital letter
outside `SENTINEL_PREFIX`, and a key has none, so two pieces never join into a sentinel the
generator did not place. The property tests run under the profile `tests/hypothesis_profiles.py`
loads.

Run: uv run pytest scripts/renderer/tests/secrets/test_renderer_placeholder_properties.py
"""

import json
from typing import NamedTuple

from hypothesis import given
from hypothesis import strategies as st

from renderer.renderer_lib.placeholders import (
    SENTINEL_PREFIX,
    SUBSTITUTE_OPT_OUT,
    Sentinels,
    references,
    sentinel,
)

# Keys that are prefixes of one another, the case the alternation's longest-first order exists for.
PREFIX_KEYS = ["a", "ab", "ab_c"]
NEAR_MISSES = [
    SENTINEL_PREFIX,
    SENTINEL_PREFIX + "a__EN",
    SENTINEL_PREFIX + "Q__END",
    "HOMELAB_SECRET_a__END",
]


class Piece(NamedTuple):
    """A literal chunk when `key` is None, otherwise the sentinel of `key`."""

    text: str
    key: str | None


def envsubst(text: str, values: dict[str, str]) -> str:
    """Flux's substitution: `$$` is one `$`, `${x}` is `values[x]`, any other `$` is itself."""
    out, i = [], 0
    while i < len(text):
        if text.startswith("$$", i):
            out.append("$")
            i += 2
        elif text.startswith("${", i):
            close = text.index("}", i + 2)
            out.append(values[text[i + 2 : close]])
            i = close + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


_literal = st.one_of(
    st.sampled_from(["$", "$$", "${", "}", "$${", *NEAR_MISSES]),
    st.text(alphabet="ab${}-_ \n"),
)


@st.composite
def _texts(draw) -> tuple[list[str], list[Piece]]:
    drawn = draw(st.lists(st.from_regex(r"[a-z][a-z0-9_]{0,6}", fullmatch=True)))
    keys = sorted(set(PREFIX_KEYS + drawn))
    pieces = draw(
        st.lists(
            st.one_of(
                _literal.map(lambda t: Piece(t, None)),
                st.sampled_from(keys).map(lambda k: Piece(sentinel(k), k)),
            )
        )
    )
    return keys, pieces


def _values(keys: list[str]) -> dict[str, str]:
    return {key: f"<{key}>" for key in keys}


def _substituted(pieces: list[Piece], values: dict[str, str]) -> str:
    return "".join(p.text if p.key is None else values[p.key] for p in pieces)


@given(_texts())
def test_envsubst_over_resolve_gives_back_the_text_with_each_value(drawn):
    keys, pieces = drawn
    text = "".join(p.text for p in pieces)
    resolved = Sentinels(keys).resolve(text)
    assert envsubst(resolved, _values(keys)) == _substituted(pieces, _values(keys))


@given(_texts())
def test_resolve_leaves_a_reference_for_each_sentinel_in_order(drawn):
    keys, pieces = drawn
    resolved = Sentinels(keys).resolve("".join(p.text for p in pieces))
    assert list(references(resolved)) == [p.key for p in pieces if p.key is not None]


def _opted_out(body: str) -> str:
    return (
        "kind: CustomResourceDefinition\nmetadata:\n  name: c\n  annotations:\n"
        f"    {SUBSTITUTE_OPT_OUT}: disabled\ndescription: {json.dumps(body)}\n"
    )


@given(_texts(), st.lists(st.booleans(), min_size=1, max_size=4))
def test_finish_keeps_every_opted_out_document_and_resolves_the_rest(drawn, opt_outs):
    keys, pieces = drawn
    body = "".join(p.text for p in pieces)
    sentinels = Sentinels(keys)
    docs = [_opted_out(body) if off else f"data: x\n{body}\n" for off in opt_outs]
    finished = sentinels.finish("---\n".join(docs))
    assert finished == "---\n".join(
        doc if off else sentinels.resolve(doc)
        for doc, off in zip(docs, opt_outs, strict=True)
    )
