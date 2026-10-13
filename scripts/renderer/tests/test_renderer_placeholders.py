"""Placeholder mode's passes and its per-directory key sets (target slice R2).

Run: uv run pytest scripts/renderer/tests/test_renderer_placeholders.py
"""

from renderer import homelab_render
from renderer.renderer_lib.cluster import RenderedTag
from renderer.renderer_lib.placeholders import (
    apply_placeholders,
    render_placeholders,
    sentinel,
)

KEY = "probe_key"
# Shell `${HOME}`, a bare `$$`, a literal `$$${x}`, a `$` written before a secret reference,
# and the reference itself, as a template renders them in placeholder mode.
TEXT = (
    "a: ${HOME}\nb: $$\nc: $$${x}\nd: $"
    + sentinel(KEY)
    + "\ne: "
    + sentinel(KEY)
    + "\n"
)
WITHOUT_SECRET = "a: ${HOME}\nb: $$\nc: $$${x}\n"


def test_a_directory_with_a_secret_escapes_and_one_without_does_not():
    rendered = [
        RenderedTag("with-secret", {"f.yaml": TEXT}),
        RenderedTag("without-secret", {"f.yaml": WITHOUT_SECRET}),
    ]
    result = apply_placeholders(rendered, [KEY])

    assert result.key_sets == {
        "with-secret": frozenset({KEY}),
        "without-secret": frozenset(),
    }
    assert result.substituting() == ["with-secret"]
    escaped, untouched = (tag.files["f.yaml"] for tag in result.tags)
    assert escaped == (
        "a: $${HOME}\nb: $$$\nc: $$$$$${x}\nd: $$${" + KEY + "}\ne: ${" + KEY + "}\n"
    )
    # Flux applies a directory with an empty key set with no postBuild, so an escape there
    # would reach the cluster as a literal `$${`.
    assert untouched == WITHOUT_SECRET


def test_a_deferred_directory_gets_its_own_key_set():
    rendered = [RenderedTag("t", {"f.yaml": "a: x\n"}, {"g.yaml": TEXT})]
    result = apply_placeholders(rendered, [KEY])
    assert result.key_sets == {"t": frozenset(), "t-deferred": frozenset({KEY})}
    (tag,) = result.tags
    assert tag.files == {"f.yaml": "a: x\n"}
    assert "${" + KEY + "}" in tag.deferred["g.yaml"]


def test_pihole_substitutes_and_its_deferred_directory_does_not():
    # The named member of the rule above in the real tree: pihole's Secret holds keys, and
    # the second instance it defers holds none.
    result = render_placeholders(["pihole"], homelab_render.secret_keys())
    assert result.key_sets["pihole"]
    assert result.key_sets["pihole-deferred"] == frozenset()
    assert result.substituting() == ["pihole"]


STANDALONE = {
    "plain": "main: " + sentinel(KEY) + "\n",
    "quoted": 'k: "' + sentinel(KEY) + '"\n',
    "block": "k: |-\n  " + sentinel(KEY) + "\n",
}
NOT_STANDALONE = {
    "prefixed": "main: x-" + sentinel(KEY) + "\n",
    # Flux types only the outer scalar, so a placeholder inside a longer block is text.
    "block-with-more-text": "config: |-\n  host: " + sentinel(KEY) + "\n  port: 1\n",
}


def test_a_placeholder_that_is_a_whole_scalar_is_flagged():
    for shape, text in STANDALONE.items():
        result = apply_placeholders([RenderedTag("t", {"f.yaml": text})], [KEY])
        assert result.standalone == {"t": frozenset({KEY})}, shape


def test_a_placeholder_inside_more_text_is_clean():
    for shape, text in NOT_STANDALONE.items():
        result = apply_placeholders([RenderedTag("t", {"f.yaml": text})], [KEY])
        assert result.key_sets == {"t": frozenset({KEY})}, shape
        assert result.standalone == {"t": frozenset()}, shape
