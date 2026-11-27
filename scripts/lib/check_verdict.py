"""A health reader's verdict, written onto its verdict ConfigMap for kube-state-metrics.

A health CronJob records each verdict as `homelab/check-*` annotations on a ConfigMap of its
own. kube-state-metrics exports `homelab/check-verdict` as a label of
`kube_configmap_annotations`, which the `pod-producers` alert rules read. The time and the
message are for a human reading the object. The rules measure freshness from the CronJob's
last successful run instead, because an annotation is a label and cannot be compared with the
clock.

The module imports the stdlib alone, because it ships beside the readers that call it: each
health ConfigMap mounts it at `lib/check_verdict.py`.

Typical usage example:

    failure = export_verdict(kubectl, "configarr-health-verdict", "configarr-health",
                             "up", msg, time.time())
"""

import json
from collections.abc import Callable
from datetime import datetime, timezone


def export_verdict(
    kubectl: Callable[..., tuple[int, str]],
    configmap: str,
    field_manager: str,
    status: str,
    msg: str,
    now: float,
) -> str | None:
    """Patches the verdict, the run time and the message onto `configmap`'s annotations.

    Args:
        kubectl: `kubectl(*args) -> (rc, output)`, as `host_lib.kubectl_runner` returns.
        configmap: the verdict ConfigMap, in the namespace `kubectl` is bound to.
        field_manager: the manager the patch is recorded under, the reader's log tag.
        status: `up` or `down`.
        msg: the verdict's message, cut to 200 characters.
        now: the run time as a Unix timestamp.

    Returns:
        None when the API server accepted the patch, else a line naming why it did not.
    """
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    patch = {
        "metadata": {
            "annotations": {
                "homelab/check-verdict": status,
                "homelab/check-last-run": stamp,
                "homelab/check-message": msg[:200],
            }
        }
    }
    rc, out = kubectl(
        "patch",
        "configmap",
        configmap,
        "--type=merge",
        # Flux's server-side apply strips fields owned by a manager named `kubectl*`, and the
        # default here is `kubectl-patch`, so the annotations vanish at every apply.
        "--field-manager=" + field_manager,
        "--patch=" + json.dumps(patch),
    )
    if rc == 0:
        return None
    return "export failed: cannot patch %s: %s" % (configmap, out.strip()[:200])
