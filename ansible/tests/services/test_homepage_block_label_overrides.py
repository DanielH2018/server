#!/usr/bin/env python3
"""A renamed block label in custom.css must still point at the field it renames.

homepage takes a block's heading from an i18n lookup baked into the image, and no config key
overrides it, so four tiles rename their headings in CSS: the label text is collapsed with
`font-size: 0` and the replacement arrives as a `::after` `content`. The tile is matched by
href, which survives a reorder, but the BLOCK is matched by `:nth-child`, and a widget renders
its blocks in the order its component declares them — which is the order of `fields:` in
services.yaml.

So the override index and the `fields:` list are one fact in two files. Reorder `fields:` alone
and the tile renders "Charge" over the status string; drop a field and the override lands on a
block that no longer exists, or on nothing at all. Neither errors: the CSS still parses, the
YAML is still valid, the pod stays 1/1, and only reading the tile shows it.

Both files are read RENDERED, out of homepage's config Secret (`_homepage_config`). The previous
form parsed `services.yaml.j2` with a line scanner that tracked the most recent `href:` and
matched `fields: [...]` with a regex, because a Jinja template is not loadable YAML — so a
`fields:` list written as a YAML block sequence rather than inline would have dropped out of the
census silently. Rendered, `fields:` is a Python list and the tile is a dict.

EXPECTED_LABELS below is the third copy on purpose. It pins which upstream field each override
is renaming, so a `fields:` reorder fails here rather than being silently absorbed by a test
that only compares the two files to each other.

Run: uv run pytest ansible/tests/services/test_homepage_block_label_overrides.py
"""

import re

from _homepage_config import custom_css, tiles

# (href fragment, 1-based block index) -> the upstream field that block renders.
# The rename itself is in the CSS; this table is what the rename is renaming.
EXPECTED_LABELS = {
    ("uptime-kuma.", 1): "up",
    ("uptime-kuma.", 2): "down",
    ("speedtest.", 1): "download",
    ("karakeep.", 1): "bookmarks",
    ("peanut.", 1): "battery_charge",
    ("peanut.", 2): "ups_status",
    ("qBittorrent.", 2): "download",
}

# li.service:has(a[href*="peanut."]) .service-block:nth-child(2) > .font-bold::after {
#   content: "Status";
OVERRIDE_RE = re.compile(
    r'li\.service:has\(a\[href\*="([^"]+)"\]\)\s+'
    r"\.service-block:nth-child\((\d+)\)\s*>\s*\.font-bold::after\s*\{\s*\n"
    r'\s*content:\s*"([^"]*)";',
)


def css_overrides(text: str) -> dict[tuple[str, int], str]:
    """(href fragment, block index) -> the replacement heading, from the CSS."""
    return {
        (fragment, int(index)): content
        for fragment, index, content in OVERRIDE_RE.findall(text)
    }


def tile_fields() -> dict[str, list[str]]:
    """href -> its widget's `fields:` list, for every tile that declares one.

    The CSS matches a tile by the `href` the pod serves, so the census is keyed by the same
    value rather than by the tile's display name.
    """
    fields = {}
    for _name, body in tiles():
        widget = body.get("widget") or {}
        href, names = body.get("href"), widget.get("fields")
        if href and isinstance(names, list):
            fields[href] = names
    return fields


def resolve(fragment: str, fields: dict[str, list[str]]) -> list[str]:
    """The `fields:` list of the one tile whose href contains `fragment`."""
    matches = [names for href, names in fields.items() if fragment in href]
    assert len(matches) == 1, (
        f"href fragment {fragment!r} matches {len(matches)} tiles with a `fields:` list, "
        "so the CSS selector renames the wrong tile or none at all"
    )
    return matches[0]


def test_every_expected_override_is_present_in_the_css():
    """Non-vacuity: the parse must find each override, not an empty set that passes."""
    found = css_overrides(custom_css())
    missing = set(EXPECTED_LABELS) - set(found)
    assert not missing, f"label overrides missing from custom.css: {sorted(missing)}"


def test_no_override_renames_a_block_the_table_does_not_cover():
    """An override added without a table entry has nothing pinning what it renames."""
    extra = set(css_overrides(custom_css())) - set(EXPECTED_LABELS)
    assert not extra, (
        f"custom.css renames blocks EXPECTED_LABELS does not cover: {sorted(extra)} — "
        "add them here so a fields: reorder fails this test"
    )


def misdirected_overrides(fields: dict[str, list[str]]) -> list[str]:
    """One message per override whose block index does not land on the field it renames.

    Pure so the rejecting halves below can hand it a swapped or truncated `fields:` list. Taking
    the real census and a synthetic one through the same function is what makes a green run on
    the real tree evidence that the comparison still fires.
    """
    out = []
    for (fragment, index), expected in sorted(EXPECTED_LABELS.items()):
        names = resolve(fragment, fields)
        if len(names) < index:
            out.append(
                f"the CSS renames block {index} of the {fragment!r} tile, but its fields: list "
                f"has only {len(names)} entries ({names}) — the override lands on nothing"
            )
        elif names[index - 1] != expected:
            out.append(
                f"block {index} of the {fragment!r} tile is {names[index - 1]!r} but the CSS "
                f"renames it as though it were {expected!r} — the tile would show the new "
                f"heading over the wrong number"
            )
    return out


def test_each_override_index_lands_on_the_field_it_renames():
    """Block N of a tile must be the field EXPECTED_LABELS says the override renames."""
    assert misdirected_overrides(tile_fields()) == []


def _tile_fields_with(fragment: str, names: list[str]) -> dict[str, list[str]]:
    """The real census with the `fields:` list of the `fragment` tile replaced by `names`.

    Keyed off the href the render actually produced, so a change to the tile's URL reshapes the
    fixture with it rather than leaving two tiles matching `fragment`.
    """
    fields = dict(tile_fields())
    href = next(h for h in fields if fragment in h)
    fields[href] = names
    return fields


def test_a_reordered_fields_list_is_rejected():
    """The red half: swapping a tile's fields must fail the index check.

    Without this there is no evidence the check above can fail — it passes whether or not the
    comparison is doing anything. Both of the UPS tile's overrides must be flagged, because the
    swap moves each of its two fields onto the other's heading.
    """
    swapped = _tile_fields_with("peanut.", ["ups_status", "battery_charge"])
    flagged = [m for m in misdirected_overrides(swapped) if "peanut." in m]
    assert len(flagged) == 2, flagged


def test_an_override_landing_past_the_end_of_a_fields_list_is_rejected():
    """The other way an override goes wrong: the block it renames no longer exists."""
    truncated = _tile_fields_with("peanut.", ["battery_charge"])
    flagged = [m for m in misdirected_overrides(truncated) if "lands on nothing" in m]
    assert len(flagged) == 1, flagged
