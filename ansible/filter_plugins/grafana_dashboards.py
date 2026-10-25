"""Ansible filter plugin adding the deploy-annotation queries to a provisioned Grafana dashboard.

observability's `templates/grafana-dashboards.yaml.j2` passes every board under
`files/dashboards/` through `grafana_deploy_annotated` as it renders the board's ConfigMap, so
the ConfigMap is a render and no task stages the boards on the node (D5's imperative-ConfigMap
row). There are two queries. `expr` reads the syslog lines a host-side deploy writes;
`flux_expr` reads the line the soak Job prints when a Flux promotion passes. A LogQL log query
has one stream selector, so one query cannot read both.

Grafana has no global annotation setting: an annotation query lives in each dashboard's own
JSON, and the boards here ship `"annotations": {"list": []}`. Editing every file by hand, and
every future board too, is the kind of fan-out that gets half-done, so the query is injected at
render time instead.

The source JSON under `files/dashboards/` is never touched, which matters because
`scripts/grafana/export_grafana_dashboards.py` round-trips boards from the Grafana UI back into
it. An injected annotation must not become something a UI export commits back as source, so
the injection is idempotent by name as well: a board that already declares an annotation with
a given name keeps it exactly as it is.

No Ansible import at load time, so the tests call the same function the playbook runs.
`FilterModule` registers it through `_as_filter`, which re-raises a `ValueError` as
`AnsibleFilterError("grafana_deploy_annotated: ...")` and fails the render task before any
apply.
"""

import json
import sys as _sys
from pathlib import Path as _Path

# Ansible loads a plugin by file path, so `filter_errors` beside it imports by bare name only
# once this directory is on sys.path.
if str(_Path(__file__).resolve().parent) not in _sys.path:
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))

from filter_errors import _as_filter

# The name is the idempotency key. Renaming it re-injects alongside the old one rather than
# replacing it, so treat each as a fixed identifier rather than a label to tune.
ANNOTATION_NAME = "Deploys"
FLUX_ANNOTATION_NAME = "Flux deploys"


def build_annotation(
    uid: str, expr: str, name: str = ANNOTATION_NAME, ref_id: str = "DeployAnno"
) -> dict:
    """The annotation query, as Grafana's dashboard JSON expects it.

    `logfmt` is what makes the text useful: without it the annotation shows the whole raw
    syslog line, and with it `textFormat` can pull out just the service list.
    """
    return {
        "name": name,
        "datasource": {"type": "loki", "uid": uid},
        "enable": True,
        "hide": False,
        "iconColor": "purple",
        "target": {"expr": expr, "refId": ref_id, "queryType": "range"},
        "textFormat": "{{services}}",
        # `result` is ok, fail or rollback (#6164), so a failed deploy never reads as a success.
        "titleFormat": "deploy {{result}}",
    }


def inject(doc: dict, annotation: dict) -> bool:
    """Add `annotation` to a dashboard doc. True if the doc changed.

    Grafana tolerates a missing `annotations` key, but the ConfigMap is easier to diff when
    every board has the same shape, so the container is created when absent.
    """
    annotations = doc.setdefault("annotations", {})
    entries = annotations.setdefault("list", [])

    if any(entry.get("name") == annotation["name"] for entry in entries):
        return False

    entries.append(annotation)
    return True


def grafana_deploy_annotated(text: str, uid: str, expr: str, flux_expr: str) -> str:
    """The dashboard JSON `text` with both deploy-annotation queries injected.

    Args:
        text: One board's JSON, as `files/dashboards/` holds it.
        uid: The `loki-homelab` datasource uid Grafana provisions.
        expr: The LogQL query selecting a host-side deploy's syslog lines.
        flux_expr: The LogQL query selecting the soak Job's deploy line.

    Returns:
        The board with sorted keys and a two-space indent, ending in one newline. The output is
        a pure function of the input, so an unchanged board renders the same ConfigMap and
        Grafana reloads nothing.

    Raises:
        ValueError: `text` is not a JSON object.
    """
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError(f"a dashboard is a JSON object, got {type(doc).__name__}")
    inject(doc, build_annotation(uid, expr))
    inject(
        doc, build_annotation(uid, flux_expr, FLUX_ANNOTATION_NAME, "FluxDeployAnno")
    )
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self):
        return {"grafana_deploy_annotated": _as_filter(grafana_deploy_annotated)}
