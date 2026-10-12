"""Ansible filter plugin that fits the vendored Flux install to this cluster's workload guards.

The flux role vendors `flux install --export` unedited in `roles/k8s/flux/files/`, so a
regeneration diffs as upstream's change alone. The fleet guards in `ansible/tests/k8s/` then
read the rendered Deployments, and upstream's differ from them in four ways. This filter
closes each one at render time:

- the role's `flux_pod_patch` is merged into every Deployment, for the uid pin,
  `enableServiceLinks` and `revisionHistoryLimit`;
- a Deployment that leaves its strategy implicit gets `RollingUpdate`, and a stated one, such
  as source-controller's `Recreate`, is kept;
- each pod runs at the class the `flux` entry's tier names, never upstream's
  `system-cluster-critical`;
- each generic volume name (`data`, `tmp`, `temp`) is prefixed with its Deployment's name, in
  `volumes` and in every container's `volumeMounts`.

A merge patch cannot rename a list item, which is why the rename is code rather than patch
data. Each renamed item is written with `name` as its first key, so a guard that reads the
rendered `- name:` lines sees the new names.

No Ansible import at load time, so the tests call the function the playbook runs.
"""

import copy
import json

import yaml

import sys as _sys
from pathlib import Path as _Path

# Ansible loads a plugin by file path, so its siblings import by bare name only once this
# directory is on the path (#4527).
if str(_Path(__file__).resolve().parent) not in _sys.path:
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))

from filter_errors import _as_filter
from role_label import _Dumper, _Loader


def _merge(base: dict, patch: dict) -> None:
    """Merge `patch` into `base` in place: a dict merges key by key, any other value replaces."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def _named_first(item: dict, name: str) -> dict:
    """`item` with its `name` replaced by `name`, written as the first key."""
    return {"name": name, **{k: v for k, v in item.items() if k != "name"}}


def flux_components(
    text: str, pod_patch: dict, priority_class: str, volume_suffixes: dict
) -> str:
    """The vendored install `text`, re-serialised with every Deployment fitted to the guards.

    Args:
        text: The vendored multi-document manifest.
        pod_patch: A Deployment-shaped merge patch applied to every Deployment.
        priority_class: The PriorityClass every controller pod runs at.
        volume_suffixes: Upstream volume name to the suffix its renamed volume takes after
            `<deployment name>-`.

    Returns:
        The documents in their vendored order, with only the Deployments changed.

    Raises:
        ValueError: The text holds no Deployment, or a Deployment mounts a volume that
            `volume_suffixes` does not name. An unnamed volume is a regeneration that added
            one, and rendering it unrenamed would trip the volume-name guard much later.
    """
    # ansible-core hands templated values over as tagged builtin subclasses, which SafeDumper
    # refuses to represent. A JSON round trip leaves plain dicts, lists and strings.
    pod_patch, priority_class, volume_suffixes = json.loads(
        json.dumps([pod_patch, priority_class, volume_suffixes])
    )
    docs = [doc for doc in yaml.load_all(str(text), Loader=_Loader) if doc is not None]
    deployments = [doc for doc in docs if doc.get("kind") == "Deployment"]
    if not deployments:
        raise ValueError(
            "the vendored install holds no Deployment, so no controller would run"
        )
    for deployment in deployments:
        name = deployment["metadata"]["name"]
        _merge(deployment, pod_patch)
        spec = deployment["spec"]
        spec.setdefault("strategy", {"type": "RollingUpdate"})
        pod = spec["template"]["spec"]
        pod["priorityClassName"] = priority_class
        renames = {}
        for volume in pod.get("volumes", []):
            if volume["name"] not in volume_suffixes:
                raise ValueError(
                    f"{name} mounts volume {volume['name']!r}, which the volume suffixes "
                    f"do not name ({sorted(volume_suffixes)}). Add its suffix to the role's "
                    "defaults"
                )
            renames[volume["name"]] = f"{name}-{volume_suffixes[volume['name']]}"
        if "volumes" in pod:
            pod["volumes"] = [
                _named_first(v, renames[v["name"]]) for v in pod["volumes"]
            ]
        for container in pod.get("initContainers", []) + pod["containers"]:
            if "volumeMounts" in container:
                container["volumeMounts"] = [
                    _named_first(m, renames[m["name"]])
                    for m in container["volumeMounts"]
                ]
    return yaml.dump_all(
        docs, Dumper=_Dumper, sort_keys=False, default_flow_style=False, width=4096
    )


class FilterModule:
    def filters(self):
        return {"flux_components": _as_filter(flux_components)}
