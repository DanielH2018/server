"""Pure logic for the cluster-API container tools.

The Phase G successors to the Docker-socket tools that went dark at the k8s rehome —
Security M1 bars the cluster from the Docker plane, so these read the Kubernetes API
with the pod's own read-only ServiceAccount instead.

Same contract as safe_reads.py: everything here is unit-tested offline; app.py is
the wiring. Nothing in this module performs I/O.
"""

from __future__ import annotations

import re

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")


def k8s_name_valid(name: str) -> bool:
    """RFC-1123-ish guard for pod/namespace names used in API paths.

    Same job as safe_reads.container_ref_valid: a name is interpolated into a URL
    path, so anything that could add path segments or query strings is rejected.
    """
    return bool(name) and bool(_NAME_RE.match(name))


def parse_pod_list(resp: dict) -> list[dict]:
    """Reduce a /api/v1/pods response to per-pod health rows (no spec, no env)."""
    rows = []
    for item in resp.get("items") or []:
        meta = item.get("metadata") or {}
        spec = item.get("spec") or {}
        status = item.get("status") or {}
        statuses = status.get("containerStatuses") or []
        rows.append(
            {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "node": spec.get("nodeName"),
                "phase": status.get("phase"),
                "ready": "%d/%d"
                % (
                    sum(1 for c in statuses if c.get("ready")),
                    len(statuses),
                ),
                "restarts": sum(c.get("restartCount") or 0 for c in statuses),
                "started": status.get("startTime"),
            }
        )
    return rows


def parse_workloads(deployments: dict, daemonsets: dict) -> list[dict]:
    """Merge deployments + daemonsets into {kind, name, ready/desired, images} rows."""
    rows = []
    for item in deployments.get("items") or []:
        meta = item.get("metadata") or {}
        status = item.get("status") or {}
        rows.append(
            {
                "kind": "Deployment",
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "ready": status.get("readyReplicas") or 0,
                "desired": status.get("replicas") or 0,
                "images": _pod_template_images(item),
            }
        )
    for item in daemonsets.get("items") or []:
        meta = item.get("metadata") or {}
        status = item.get("status") or {}
        rows.append(
            {
                "kind": "DaemonSet",
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "ready": status.get("numberReady") or 0,
                "desired": status.get("desiredNumberScheduled") or 0,
                "images": _pod_template_images(item),
            }
        )
    return rows


def _pod_template_images(item: dict) -> list[str]:
    template = ((item.get("spec") or {}).get("template") or {}).get("spec") or {}
    return [c.get("image") for c in template.get("containers") or []]


def parse_nodes(resp: dict) -> list[dict]:
    """Reduce /api/v1/nodes to {name, ready, schedulable, kubelet} rows."""
    rows = []
    for item in resp.get("items") or []:
        meta = item.get("metadata") or {}
        spec = item.get("spec") or {}
        status = item.get("status") or {}
        ready = next(
            (
                c.get("status")
                for c in status.get("conditions") or []
                if c.get("type") == "Ready"
            ),
            "Unknown",
        )
        rows.append(
            {
                "name": meta.get("name"),
                "ready": ready,
                "schedulable": not spec.get("unschedulable", False),
                "kubelet": (status.get("nodeInfo") or {}).get("kubeletVersion"),
            }
        )
    return rows


# Metadata-only projection for claude_code_events. The claude-otel Loki holds prompts,
# responses and tool output VERBATIM (content logging is on, by design), and KL1's
# whole boundary is that this content never gets a LAN-reachable path — so rows leaving
# through the (bearer-gated, LAN-routed) MCP carry ONLY these fields. A whitelist, not
# a blacklist: an OTLP attribute added upstream stays withheld until named here. The
# log line/body is never returned in any form.
CLAUDE_EVENT_FIELDS = (
    "event_name",
    "tool_name",
    "decision",
    "source",
    "success",
    "model",
    "permission_mode",
    "session_id",
    "duration_ms",
    "error",
    "service_name",
)


def claude_event_rows(parsed: list[dict]) -> list[dict]:
    """Project parse_loki rows to whitelisted metadata; drop the line body entirely."""
    rows = []
    for row in parsed:
        labels = row.get("labels") or {}
        projected = {k: labels[k] for k in CLAUDE_EVENT_FIELDS if k in labels}
        projected["ts"] = row.get("ts")
        rows.append(projected)
    return rows


def claude_loki_base_or_raise(url: str) -> str:
    """The claude-otel Loki base URL, or a clear error while the tool is dark."""
    if not url:
        raise RuntimeError(
            "claude_code_events is dark: CLAUDE_LOKI_URL is unset, so this deploy "
            "cannot reach the claude-otel Loki. claude_code_usage (metrics) and "
            "query_logs (homelab Loki) still work."
        )
    return url
