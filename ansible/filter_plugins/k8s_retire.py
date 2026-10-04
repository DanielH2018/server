"""Ansible filter plugin for tearing down a retired k8s service (ansible/tasks/k8s_retire.yml).

`k8s_staged_objects` lists the objects a retired service's staged manifest files declare, and
`k8s_cluster_scoped` picks out the ones whose kind the cluster serves without a namespace: a
Namespace, a ClusterRole, a PersistentVolume, a StorageClass. The retire play deletes every
object its files declare, so it refuses before the delete when this returns any (#3521).
"""

import yaml
from ansible.errors import AnsibleFilterError


def k8s_staged_objects(file_texts):
    """Every object the given manifest texts declare, as `{kind, name}`, a `List` expanded."""
    objects = []
    for text in file_texts:
        for doc in yaml.safe_load_all(text):
            if not isinstance(doc, dict) or "kind" not in doc:
                continue
            items = doc.get("items") if doc["kind"].endswith("List") else [doc]
            for item in items or []:
                name = (item.get("metadata") or {}).get("name", "?")
                objects.append({"kind": item["kind"], "name": name})
    return objects


def k8s_cluster_scoped(objects, api_resources, allowed=()):
    """The `Kind/name` of each object whose kind is cluster-scoped and not in `allowed`.

    Args:
        objects: `k8s_staged_objects` output.
        api_resources: stdout of `kubectl api-resources --namespaced=false --no-headers`,
            whose last column is the KIND.
        allowed: `Kind/name` strings the retire entry deletes on purpose.

    Raises:
        AnsibleFilterError: when `api_resources` names no Namespace kind. An empty or
            unparsed listing would otherwise read as "nothing is cluster-scoped" and let the
            delete through.
    """
    kinds = {line.split()[-1] for line in api_resources.splitlines() if line.strip()}
    if "Namespace" not in kinds:
        raise AnsibleFilterError(
            "the api-resources listing names no Namespace kind, so it cannot say which "
            f"staged objects are cluster-scoped: {api_resources[:200]!r}"
        )
    return [
        f"{o['kind']}/{o['name']}"
        for o in objects
        if o["kind"] in kinds and f"{o['kind']}/{o['name']}" not in allowed
    ]


class FilterModule:
    def filters(self):
        return {
            "k8s_staged_objects": k8s_staged_objects,
            "k8s_cluster_scoped": k8s_cluster_scoped,
        }
