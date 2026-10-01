#!/usr/bin/env python3
"""The Headlamp tile's widget mappings must stay in the query's operand order.

homepage's `customapi` widget takes ONE url, so the Headlamp tile's cluster counters arrive as
a single PromQL query whose operands are joined with `or`. In `display: block` mode homepage
pairs a value to its label by ARRAY POSITION — `data.result.0.value.1` is whatever operand
Prometheus happened to return first — so the label list and the operand order in
`homepage_k8s_headlamp_cluster_query` are one fact written in two places.

Edit either alone and the tile keeps rendering plausible numbers under the wrong headings.
Nothing else catches it: the YAML is valid, the manifest schema is satisfied, the URL still
returns HTTP 200, and `probe.py health homepage` reports a healthy pod. Only a human reading a
three-figure count under DOWN would notice, and only if they knew the cluster was healthy.

Both halves come out of the RENDERED tile (`_homepage_config`), which is where the pod reads
them: the query arrives urlencoded inside the widget's `url`, and the mappings are a list beside
it. The previous form read the query from `defaults/main.yml` and regex-scanned
`services.yaml.j2` for `- field:`/`label:` line pairs, so it compared the default against the
template rather than the two values the pod is served — and a host_vars override of the query,
or a mapping list reformatted, was invisible to it.

This is the executable form of the `# DECIDED:` marker in services.yaml.j2, which accepts
positional pairing so the tile matches every other widget on the dashboard.

Run: uv run pytest ansible/tests/services/test_headlamp_widget_mapping_order.py
"""

import re
from urllib.parse import parse_qs, urlparse

from _homepage_config import tiles

TILE = "Headlamp"

# label_replace(<expr>, "k", "<name>", "", "") — capture the assigned name. Anchored on the
# `"k",` destination label rather than on `label_replace(`, because the counter expressions
# themselves contain quotes (condition="Ready") and a non-greedy span across them would stop
# at the first inner quote and match only the one counter that has none.
LABEL_REPLACE_RE = re.compile(r'"k",\s*"([^"]+)"')
# `data.result.<n>.value.1` — the array position a mapping reads.
RESULT_FIELD_RE = re.compile(r"^data\.result\.(\d+)\.value\.1$")


def headlamp_widget() -> dict:
    """The Headlamp tile's widget config, as the pod is served it."""
    bodies = [body for name, body in tiles() if name == TILE]
    assert len(bodies) == 1, (
        f"expected one {TILE} tile in services.yaml, got {len(bodies)}"
    )
    widget = bodies[0].get("widget") or {}
    assert widget.get("type") == "customapi", (
        f"the {TILE} tile is no longer a customapi widget, got {widget.get('type')!r} — "
        "positional pairing is a customapi/display:block property"
    )
    return widget


def query_names() -> list[str]:
    """The counter names in the order the query's `or` operands produce them."""
    url = urlparse(headlamp_widget()["url"])
    queries = parse_qs(url.query)["query"]
    assert len(queries) == 1, (
        f"expected one PromQL query in the widget url, got {queries}"
    )
    return LABEL_REPLACE_RE.findall(queries[0])


def widget_mappings() -> list[tuple[int, str]]:
    """(array index, label) for each positional block mapping on the Headlamp tile."""
    out = []
    for mapping in headlamp_widget().get("mappings") or []:
        if match := RESULT_FIELD_RE.match(str(mapping.get("field", ""))):
            out.append((int(match.group(1)), mapping.get("label")))
    return out


def test_query_names_are_distinct():
    """`or` is set union over series signatures, so identical labels collapse the result.

    Bare `vector()` samples all carry the empty signature; without a distinguishing label the
    union returns ONE series and the tile renders a single number. Measured 2026-08-23:
    dropping label_replace returned a `result` of length 1, not 4. Duplicate names would
    silently reintroduce exactly that collapse for the duplicated pair.
    """
    names = query_names()
    assert names, "no label_replace names found in the Headlamp widget's query"
    assert len(names) == len(set(names)), (
        f"duplicate counter names collapse the union: {names}"
    )


def test_mapping_indices_are_dense_and_ordered():
    """The mappings must index 0..n-1 in order, so position N really is the Nth operand."""
    indices = [i for i, _ in widget_mappings()]
    assert indices == list(range(len(indices))), (
        f"Headlamp widget mappings must index 0..n-1 in file order, got {indices}"
    )


def mislabelled_blocks(names, mappings) -> list[str]:
    """One message per block whose label does not name the operand at its array position."""
    return [
        f"data.result.{index} is the query's {name!r} counter but the widget labels it "
        f"{label!r} — the tile would render that number under the wrong heading"
        for (index, label), name in zip(mappings, names, strict=True)
        if label != name
    ]


def test_mapping_labels_match_query_order():
    """Label at position N must name the query's Nth `or` operand."""
    names = query_names()
    mappings = widget_mappings()
    assert mappings, "no positional block mappings found on the Headlamp tile"
    assert len(mappings) == len(names), (
        f"{len(names)} counters in the Headlamp query but {len(mappings)} widget mappings — "
        "every counter needs a block, and every block a counter"
    )
    assert mislabelled_blocks(names, mappings) == []


def test_a_swapped_label_pair_is_flagged():
    """The rejecting half. A comparison that matched nothing would pass the test above."""
    flagged = mislabelled_blocks(["Up", "Down"], [(0, "Down"), (1, "Up")])
    assert len(flagged) == 2, flagged
