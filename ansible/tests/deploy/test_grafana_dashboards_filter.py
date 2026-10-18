"""The `grafana_deploy_annotated` filter in filter_plugins/grafana_dashboards.py.

observability's dashboard ConfigMaps pass every board through it, so these pin the injection
itself: both queries added, a board's own annotations kept, idempotent by name, and a stable
output. `test_dashboard_configmaps.py` checks the rendered ConfigMaps that carry the result.

Run: uv run pytest ansible/tests/deploy/test_grafana_dashboards_filter.py
"""

import json
from typing import Any

import pytest
from ansible.errors import AnsibleFilterError

import grafana_dashboards as gd

UID = "bf4q19tuivta8e"
EXPR = '{job="syslog"} |= "event=deploy" | logfmt'
FLUX_EXPR = '{job="k8s", namespace="flux-system", container="soak"} |= "event=deploy"'


def _annotated(doc: dict[str, Any]) -> dict:
    return json.loads(
        gd.grafana_deploy_annotated(json.dumps(doc), UID, EXPR, FLUX_EXPR)
    )


def _entries(doc: dict) -> list[tuple[str, str]]:
    return [(e["name"], e["target"]["expr"]) for e in doc["annotations"]["list"]]


def test_both_queries_are_injected_into_an_empty_list():
    doc = _annotated({"annotations": {"list": []}, "panels": []})
    assert _entries(doc) == [("Deploys", EXPR), ("Flux deploys", FLUX_EXPR)]
    assert {e["datasource"]["uid"] for e in doc["annotations"]["list"]} == {UID}


def test_a_board_with_no_annotations_key_gains_both_queries():
    assert _entries(_annotated({"panels": []})) == [
        ("Deploys", EXPR),
        ("Flux deploys", FLUX_EXPR),
    ]


def test_a_board_s_own_annotations_are_kept_ahead_of_the_injected_ones():
    doc = _annotated({"annotations": {"list": [{"name": "Alerts", "enable": True}]}})
    assert [e["name"] for e in doc["annotations"]["list"]] == [
        "Alerts",
        "Deploys",
        "Flux deploys",
    ]


def test_an_already_annotated_board_is_left_as_it_is():
    """Idempotent by name, so a UI export of an injected board does not stack a second copy.

    `scripts/grafana/export_grafana_dashboards.py` can pull a board back into files/ carrying
    the injected annotations; the next render must leave them alone.
    """
    once = gd.grafana_deploy_annotated('{"panels": []}', UID, EXPR, FLUX_EXPR)
    assert gd.grafana_deploy_annotated(once, UID, EXPR, FLUX_EXPR) == once
    assert len(json.loads(once)["annotations"]["list"]) == 2


def test_the_output_is_a_pure_function_of_the_input():
    """Sorted keys and one trailing newline, so an unchanged board renders the same bytes."""
    out = gd.grafana_deploy_annotated('{"b": 1, "a": 2}', UID, EXPR, FLUX_EXPR)
    assert out.endswith("}\n") and not out.endswith("\n\n")
    assert out.index('"a"') < out.index('"b"')
    assert gd.grafana_deploy_annotated('{"a": 2, "b": 1}', UID, EXPR, FLUX_EXPR) == out


@pytest.mark.parametrize(
    "text", ["[]", '"board"', "{not json"], ids=["list", "string", "broken"]
)
def test_a_board_that_is_not_a_json_object_fails_the_render(text):
    """A broken board fails the render task, named for the filter, rather than reaching Grafana."""
    with pytest.raises((ValueError, AnsibleFilterError)) as caught:
        gd.FilterModule().filters()["grafana_deploy_annotated"](
            text, UID, EXPR, FLUX_EXPR
        )
    assert isinstance(caught.value, AnsibleFilterError), type(caught.value)
    assert str(caught.value).startswith("grafana_deploy_annotated: ")
