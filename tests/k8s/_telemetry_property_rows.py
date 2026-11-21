"""Rendered-manifest property rows over the otel-collector that ships Claude Code's telemetry.

`test_rendered_properties.py` runs these rows beside its own; the rules for what makes a row are
in its docstring.
"""

from lib import yaml_fast

from _k8s_render import pod_spec
from _property_table import Property

_ROLE = "observability"

NODE_STAMP = {"key": "node", "value": "${env:NODE_NAME}", "action": "upsert"}
NODE_NAME_ENV = {
    "name": "NODE_NAME",
    "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}},
}


def _collector_config(role: str, tpl: str, doc: dict):
    if (
        doc.get("kind") == "ConfigMap"
        and doc["metadata"]["name"] == "otel-collector-config"
    ):
        yield "otel-collector-config", yaml_fast.safe_load(doc["data"]["config.yaml"])


def _stamps_node(config: dict, pipeline: str) -> bool:
    processors = config["processors"]
    return any(
        NODE_STAMP in processors[name].get("attributes", [])
        for name in config["service"]["pipelines"][pipeline]["processors"]
    )


def _node_stamp_offence(role: str, config: dict) -> str | None:
    if not _stamps_node(config, "logs"):
        return "the logs pipeline does not stamp `node` from NODE_NAME"
    if _stamps_node(config, "metrics"):
        return "the metrics pipeline stamps `node`, which collides with the scrape's `node`"
    return None


def _config(logs: list[str], metrics: list[str]) -> dict:
    return {
        "processors": {"batch": {}, "resource/node": {"attributes": [NODE_STAMP]}},
        "service": {
            "pipelines": {
                "logs": {"processors": logs},
                "metrics": {"processors": metrics},
            }
        },
    }


def _collector_containers(role: str, tpl: str, doc: dict):
    if doc.get("kind") == "DaemonSet" and doc["metadata"]["name"] == "otel-collector":
        for container in pod_spec(doc)["containers"]:
            yield container["name"], container


def _node_name_env_offence(role: str, container: dict) -> str | None:
    if NODE_NAME_ENV in container.get("env", []):
        return None
    return "no NODE_NAME env from spec.nodeName, so `${env:NODE_NAME}` expands empty"


TELEMETRY_PROPERTIES = (
    Property(
        name="otel-collector-stamps-node-on-logs-only",
        reason=(
            "The Claude Code cost panels read `cost_usd` from Loki's api_request events, and "
            "Loki has no other label naming the host a session ran on, so `Cost by node` "
            "groups by the `node` the collector stamps. Prometheus already sets `node` on the "
            "scraped metrics, and `resource_to_telemetry_conversion` would turn a metrics "
            "stamp into a second `node` that Prometheus renames `exported_node`."
        ),
        select=_collector_config,
        offence=_node_stamp_offence,
        red=(_ROLE, _config(["batch"], ["batch"])),
        more_red=(
            (_ROLE, _config(["resource/node", "batch"], ["resource/node", "batch"])),
        ),
        green=(_ROLE, _config(["resource/node", "batch"], ["batch"])),
        must_find=frozenset({"otel-collector-config"}),
    ),
    Property(
        name="otel-collector-reads-its-node-name",
        reason=(
            "The collector's `resource/node` processor stamps `${env:NODE_NAME}` on every log "
            "record. The collector expands an unset variable to an empty string, so without "
            "the downward-API env every event groups under an empty node."
        ),
        select=_collector_containers,
        offence=_node_name_env_offence,
        red=(_ROLE, {"name": "otel-collector", "env": []}),
        green=(_ROLE, {"name": "otel-collector", "env": [NODE_NAME_ENV]}),
        must_find=frozenset({"otel-collector"}),
    ),
)
