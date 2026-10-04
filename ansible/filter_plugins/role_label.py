"""Ansible filter plugin stamping `homelab/role: <service>` onto every object a render produced.

`k8s/manifests` prunes an armed role's dropped objects with `kubectl apply --prune -l
homelab/role=<service>` (#3388). The selector does two jobs there. It decides which live
objects the prune may delete, and it also FILTERS THE APPLY: kubectl applies only the
documents in the directory that carry the label and skips the rest without a word. So every
document an armed role stages must carry the label, its Secrets and claims included, or the
deploy stops applying that object while still reporting green.

Hand-writing the label into each template was the blocker the old `DECIDED:` marker named.
The shared `service.yaml` and `ingressroute.yaml` defaults serve dozens of roles, so the label
cannot be a literal in them. Stamping it here, after the template renders, needs no template
edit at all.

The filter also refuses an empty render. A template whose output holds no object would leave
the prune deleting every live object that file used to declare.

Only `metadata.labels` changes. A Deployment's `spec.selector` and pod-template labels stay as
rendered, so labelling a workload rolls no pods and touches no Service selector.

No Ansible import, so the tests call the same function the playbook runs. Ansible wraps a
filter's `ValueError` in its own error, which fails the render task before any apply.
"""

import yaml

ROLE_LABEL = "homelab/role"


class _Dumper(yaml.SafeDumper):
    """SafeDumper that writes a multi-line string as a `|` block, as a template would."""


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _represent_str)


def homelab_role_label(text: str, role: str) -> str:
    """`text` re-serialised with `metadata.labels["homelab/role"] = role` on every document.

    Raises ValueError on an empty render, on a document that is not a Kubernetes object, and
    on a document that already carries the label with a different value. The last one would
    hand that object to another role's prune.
    """
    if not role:
        raise ValueError("homelab_role_label needs the role name to stamp")
    # ansible-core passes a templated value as a tagged `str` subclass, which SafeDumper
    # refuses to represent ("cannot represent an object").
    role = str(role)
    docs = [doc for doc in yaml.safe_load_all(str(text)) if doc is not None]
    if not docs:
        raise ValueError(
            f"a manifest for {role} rendered no document. Pruning after an empty render "
            "would delete every live object the file used to declare, so the render is "
            "refused before the apply"
        )
    for doc in docs:
        if not isinstance(doc, dict) or "kind" not in doc:
            raise ValueError(
                f"a manifest for {role} rendered a document with no `kind`, which "
                "is not a Kubernetes object"
            )
        metadata = doc.get("metadata")
        if not isinstance(metadata, dict):
            metadata = doc["metadata"] = {}
        labels = metadata.get("labels")
        if not isinstance(labels, dict):
            labels = metadata["labels"] = {}
        held = labels.get(ROLE_LABEL)
        if held is not None and held != role:
            raise ValueError(
                f"{doc['kind']}/{metadata.get('name', '?')} already carries "
                f"{ROLE_LABEL}={held}, and {role} renders it. Two roles labelling one "
                "object would let either role's prune delete it"
            )
        labels[ROLE_LABEL] = role
    return yaml.dump_all(
        docs, Dumper=_Dumper, sort_keys=False, default_flow_style=False, width=4096
    )


class FilterModule:
    def filters(self):
        return {"homelab_role_label": homelab_role_label}
