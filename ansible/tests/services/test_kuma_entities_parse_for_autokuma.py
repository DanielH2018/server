"""No entity value carries Tera syntax AutoKuma would try to render (#2076).

AutoKuma runs every entity through the Tera template engine before it parses it
(`autokuma/src/entity.rs`, `get_entity_from_settings`, unconditional). A value that Tera cannot
render fails the whole entity, an entity that fails to parse counts as removed, and under
`ON_DELETE=delete` — kept on purpose, see the DECIDED marker in `templates/deployment.yaml.j2` —
a removed shared entity deletes every monitor that names it. That is how 107 monitors went on
2026-09-18. The Liquid templates Kuma renders at send time use the same delimiters, so they
travel inside a Tera `raw` block, which Tera strips. This walks every string in every rendered
entity and refuses a delimiter outside such a block.
"""

import re

import pytest

from _kuma_entities import _entities

TERA_DELIMITER = re.compile(r"\{\{|\{%|\{#")
RAW_BLOCK = re.compile(r"\{% raw %\}.*?\{% endraw %\}", re.DOTALL)


def _strings(value, path=""):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _strings(v, f"{path}[{i}]")


def tera_hazards(value: str) -> list[str]:
    """The Tera delimiters in `value` that no raw block covers, in order. Pure."""
    outside = RAW_BLOCK.sub("", value)
    return TERA_DELIMITER.findall(outside)


def test_no_entity_value_carries_tera_syntax_outside_a_raw_block():
    offenders = [
        f"{name}{path}: {hazards}"
        for name, entity in _entities().items()
        for path, text in _strings(entity)
        if (hazards := tera_hazards(text))
    ]
    assert not offenders, "\n".join(offenders)


def test_the_templates_are_the_values_that_needed_the_raw_block():
    # Non-vacuity: the guard has something to protect. Both notification bodies carry Liquid
    # and both are wrapped; a future value with `{{` in it must be wrapped the same way.
    entities = _entities()
    wrapped = [
        path
        for name in ("discord.json", "email.json")
        for path, text in _strings(entities[name])
        if RAW_BLOCK.search(text)
    ]
    assert sorted(wrapped) == [
        ".config.customBody",
        ".config.customSubject",
        ".config.webhookCustomBody",
    ]


@pytest.mark.parametrize(
    ("value", "hazards"),
    [
        ("plain text", []),
        ("{% raw %}{{ msg }} {% if x %}{% endraw %}", []),
        ("{{ msg }}", ["{{"]),
        ("{% raw %}{{ ok }}{% endraw %} and {% comment %}", ["{%"]),
        ("a {# note #} b", ["{#"]),
    ],
)
def test_tera_hazards_finds_delimiters_only_outside_raw(value, hazards):
    assert tera_hazards(value) == hazards
