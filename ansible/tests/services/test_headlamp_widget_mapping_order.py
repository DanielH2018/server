#!/usr/bin/env python3
"""The Headlamp tile's widget mappings must stay in the query's operand order.

homepage's `customapi` widget takes ONE url, so the Headlamp tile's four cluster counters
arrive as a single PromQL query whose operands are joined with `or`. In `display: block` mode
homepage pairs a value to its label by ARRAY POSITION — `data.result.0.value.1` is whatever
operand Prometheus happened to return first — so the label list in services.yaml.j2 and the
operand order in homepage_k8s_headlamp_cluster_query are one fact written in two places.

Edit either alone and the tile keeps rendering four plausible numbers under the wrong headings.
Nothing else catches it: the YAML is valid, the manifest schema is satisfied, the URL still
returns HTTP 200, and `probe.py health homepage` reports a healthy pod. Only a human reading
"97" under NODES would notice, and only if they knew the cluster has two nodes.

This is the executable form of the `# DECIDED:` marker in services.yaml.j2, which accepts
positional pairing so the tile matches every other widget on the dashboard.

Run: uv run pytest ansible/tests/services/test_headlamp_widget_mapping_order.py
"""

import re

import yaml
from _helpers import ANSIBLE

HOMEPAGE = ANSIBLE / "roles" / "k8s" / "homepage"
DEFAULTS = HOMEPAGE / "defaults" / "main.yml"
SERVICES = HOMEPAGE / "templates" / "services.yaml.j2"

QUERY_VAR = "homepage_k8s_headlamp_cluster_query"

# label_replace(<expr>, "k", "<name>", "", "") — capture the assigned name. Anchored on the
# `"k",` destination label rather than on `label_replace(`, because the counter expressions
# themselves contain quotes (condition="Ready") and a non-greedy span across them would stop
# at the first inner quote and match only the one counter that has none.
LABEL_REPLACE_RE = re.compile(r'"k",\s*"([^"]+)"')
# A mapping entry: `- field: data.result.<n>.value.1` followed by `label: <text>`.
MAPPING_RE = re.compile(
    r"-\s*field:\s*data\.result\.(\d+)\.value\.1\s*\n\s*label:\s*(.+?)\s*\n",
)


def query_names() -> list[str]:
    """The counter names in the order the query's `or` operands produce them."""
    query = yaml.safe_load(DEFAULTS.read_text())[QUERY_VAR]
    return LABEL_REPLACE_RE.findall(query)


def widget_mappings() -> list[tuple[int, str]]:
    """(array index, label) for each block mapping on the Headlamp tile."""
    return [(int(i), label) for i, label in MAPPING_RE.findall(SERVICES.read_text())]


def test_query_names_are_distinct():
    """`or` is set union over series signatures, so identical labels collapse the result.

    Four bare `vector()` samples all carry the empty signature; without a distinguishing label
    the union returns ONE series and the tile renders a single number. Measured 2026-08-23:
    dropping label_replace returned a `result` of length 1, not 4. Duplicate names would
    silently reintroduce exactly that collapse for the duplicated pair.
    """
    names = query_names()
    assert names, f"no label_replace names found in {QUERY_VAR}"
    assert len(names) == len(set(names)), (
        f"duplicate counter names collapse the union: {names}"
    )


def test_mapping_indices_are_dense_and_ordered():
    """The mappings must index 0..n-1 in order, so position N really is the Nth operand."""
    indices = [i for i, _ in widget_mappings()]
    assert indices == list(range(len(indices))), (
        f"Headlamp widget mappings must index 0..n-1 in file order, got {indices}"
    )


def test_mapping_labels_match_query_order():
    """Label at position N must name the query's Nth `or` operand."""
    names = query_names()
    mappings = widget_mappings()
    assert mappings, "no positional block mappings found on the Headlamp tile"
    assert len(mappings) == len(names), (
        f"{len(names)} counters in {QUERY_VAR} but {len(mappings)} widget mappings — "
        "every counter needs a block, and every block a counter"
    )
    for (index, label), name in zip(mappings, names, strict=True):
        assert label == name, (
            f"data.result.{index} is the query's {name!r} counter but the widget labels it "
            f"{label!r} — the tile would render that number under the wrong heading"
        )
