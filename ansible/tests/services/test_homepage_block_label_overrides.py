#!/usr/bin/env python3
"""A renamed block label in custom.css.j2 must still point at the field it renames.

homepage takes a block's heading from an i18n lookup baked into the image, and no config key
overrides it, so four tiles rename their headings in CSS: the label text is collapsed with
`font-size: 0` and the replacement arrives as a `::after` `content`. The tile is matched by
href, which survives a reorder, but the BLOCK is matched by `:nth-child`, and a widget renders
its blocks in the order its component declares them — which is the order of `fields:` in
services.yaml.j2.

So the override index and the `fields:` list are one fact in two files. Reorder `fields:` alone
and the tile renders "Charge" over the status string; drop a field and the override lands on a
block that no longer exists, or on nothing at all. Neither errors: the CSS still parses, the
YAML is still valid, the pod stays 1/1, and only reading the tile shows it.

EXPECTED_LABELS below is the third copy on purpose. It pins which upstream field each override
is renaming, so a `fields:` reorder fails here rather than being silently absorbed by a test
that only compares the two files to each other.

Run: uv run pytest ansible/tests/services/test_homepage_block_label_overrides.py
"""

import re

from _helpers import ANSIBLE

HOMEPAGE = ANSIBLE / "roles" / "k8s" / "homepage"
CSS = HOMEPAGE / "templates" / "config" / "custom.css.j2"
SERVICES = HOMEPAGE / "templates" / "services.yaml.j2"

# (href fragment, 1-based block index) -> the upstream field that block renders.
# The rename itself is in the CSS; this table is what the rename is renaming.
EXPECTED_LABELS = {
    ("uptime-kuma.", 1): "up",
    ("uptime-kuma.", 2): "down",
    ("speedtest.", 1): "download",
    ("karakeep.", 1): "bookmarks",
    ("peanut.", 1): "battery_charge",
    ("peanut.", 2): "ups_status",
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


def tile_fields(text: str) -> dict[str, list[str]]:
    """href -> its widget's `fields:` list, for every tile in services.yaml.j2.

    Scans line by line rather than parsing YAML: the file is a Jinja template whose values
    carry `{{ ... }}`, so it is not loadable. A tile's `href:` always precedes its `widget:`,
    so the most recent href is the one a `fields:` belongs to.
    """
    fields: dict[str, list[str]] = {}
    href = None
    for line in text.splitlines():
        if match := re.match(r"\s*href:\s*(\S+)", line):
            href = match.group(1)
        elif match := re.match(r"\s*fields:\s*\[([^\]]*)\]", line):
            if href is not None:
                fields[href] = [
                    f.strip() for f in match.group(1).split(",") if f.strip()
                ]
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
    found = css_overrides(CSS.read_text())
    missing = set(EXPECTED_LABELS) - set(found)
    assert not missing, f"label overrides missing from custom.css.j2: {sorted(missing)}"


def test_no_override_renames_a_block_the_table_does_not_cover():
    """An override added without a table entry has nothing pinning what it renames."""
    extra = set(css_overrides(CSS.read_text())) - set(EXPECTED_LABELS)
    assert not extra, (
        f"custom.css.j2 renames blocks EXPECTED_LABELS does not cover: {sorted(extra)} — "
        "add them here so a fields: reorder fails this test"
    )


def test_each_override_index_lands_on_the_field_it_renames():
    """Block N of a tile must be the field EXPECTED_LABELS says the override renames."""
    fields = tile_fields(SERVICES.read_text())
    for (fragment, index), expected in EXPECTED_LABELS.items():
        names = resolve(fragment, fields)
        assert len(names) >= index, (
            f"the CSS renames block {index} of the {fragment!r} tile, but its fields: list has "
            f"only {len(names)} entries ({names}) — the override lands on nothing"
        )
        assert names[index - 1] == expected, (
            f"block {index} of the {fragment!r} tile is {names[index - 1]!r} but the CSS "
            f"renames it as though it were {expected!r} — the tile would show the new heading "
            "over the wrong number"
        )


def test_a_reordered_fields_list_is_rejected():
    """The red half: swapping a tile's fields must fail the index check.

    Without this there is no evidence the check above can fail — both halves of the real-file
    assertion pass whether or not the comparison is doing anything.
    """
    swapped = tile_fields(
        "        - UPS:\n"
        "            href: https://peanut.example.com/\n"
        "            widget:\n"
        "                fields: [ups_status, battery_charge]\n"
    )
    names = resolve("peanut.", swapped)
    assert names[0] != EXPECTED_LABELS[("peanut.", 1)], (
        "the swapped fixture must disagree with EXPECTED_LABELS, or the real-file test proves "
        "nothing"
    )
