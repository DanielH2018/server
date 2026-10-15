"""`checksum/restart`: the pod annotation that rolls a flux-owned workload (target slice D3.3c).

Ansible's shared `rollout restart` does not exist under Flux, so the renderer stamps
`checksum/restart` on the pod template of each restart target of a flux-owned tag, and the
pod rolls when the value moves. The targets are those the shared `k8s/manifests` role
restarts: the `manifests_rollout` workload when it is non-empty, and every
`manifests_extra_rollouts` entry (`restart_targets`).

A target's value is one SHA-256 over:

- each ConfigMap and Secret its pod template names through `envFrom`, `env[].valueFrom` or
  `volumes`, from the tag's own placeholder-form render, re-serialised in canonical form, so a
  YAML comment edit moves nothing;
- the `.source` digest of every secret key those Secrets reference as `${key}`;
- for a Secret shipped as a SOPS manifest, the `.source` digest of its key, never its
  ciphertext.

A target that names no ConfigMap or Secret of its own render gets no annotation. Both render
modes hash the placeholder-form documents, never a stub, so a stub render and a placeholder
render of one tag carry the same value. `stamp` inserts the annotation as text, at the place
the YAML parser marks, so no other byte of the file moves: a re-serialised document would drop
the quotes around a `"${key}"` and change the type Flux substitutes into it.

`pod_references` is also what the outside-role reference test reads, so the checksum and that
test agree on what a pod names. The plan is docs/target-architecture/deploy/d3-secrets.md,
*Restart triggers (D3.3c)*.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path

import jinja2
import yaml

from lib import yaml_fast
from lib.k8s_context import resolve_vars
from lib.render_guard import StrictChainableUndefined
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.placeholders import Sentinels, references
from renderer.renderer_lib.task_inputs import include_vars

RESTART_ANNOTATION = "checksum/restart"
# The kinds `kubectl rollout restart` acts on, the only kinds the shared role restarts.
WORKLOAD_KINDS = frozenset({"Deployment", "DaemonSet", "StatefulSet"})
TARGET_KEYS = ("manifests_rollout", "manifests_extra_rollouts")

Ref = tuple[str, str]


def restart_targets(tag: str, context: Mapping, roles: Path = K8S_ROLES) -> list[str]:
    """The sorted workload names the shared role restarts for `tag`.

    Args:
        tag: The deploy tag, whose role is `roles/<tag>`.
        context: The play context the include's Jinja values resolve against.
        roles: The k8s roles directory; a test passes a fixture tree.

    Raises:
        RenderError: A target value does not resolve, or is not a name or a list of
            `{name: ...}` entries.
    """
    raw = include_vars(roles / tag)
    read = {k: raw[k] for k in TARGET_KEYS if k in raw}
    try:
        values = resolve_vars(
            read, {**context, **read}, undefined_cls=StrictChainableUndefined
        )
    except (ValueError, jinja2.TemplateError) as exc:
        raise RenderError(f"{tag}: its restart targets do not resolve: {exc}") from exc
    primary = values.get("manifests_rollout", tag)
    extra = values.get("manifests_extra_rollouts") or []
    if not isinstance(primary, str) or not isinstance(extra, list):
        raise RenderError(
            f"{tag}: manifests_rollout must be a name and manifests_extra_rollouts a list"
        )
    names = {primary} if primary else set()
    for entry in extra:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise RenderError(
                f"{tag}: a manifests_extra_rollouts entry has no name: {entry!r}"
            )
        names.add(str(entry["name"]))
    return sorted(names)


def pod_template(obj: object) -> dict | None:
    """The pod template of a workload document, or None for any other document."""
    if not isinstance(obj, dict) or obj.get("kind") not in WORKLOAD_KINDS:
        return None
    template = (obj.get("spec") or {}).get("template")
    return template if isinstance(template, dict) else None


def any_pod_template(obj: object) -> dict | None:
    """The pod template of any document that runs a pod: a workload, a Job or a CronJob."""
    if isinstance(obj, dict) and obj.get("kind") == "CronJob":
        job = ((obj.get("spec") or {}).get("jobTemplate") or {}).get("spec") or {}
        template = job.get("template")
    elif isinstance(obj, dict) and obj.get("kind") == "Job":
        template = (obj.get("spec") or {}).get("template")
    else:
        return pod_template(obj)
    return template if isinstance(template, dict) else None


def pod_references(template: Mapping) -> set[Ref]:
    """Each `(kind, name)` of a ConfigMap or Secret that `template` names.

    It reads `envFrom` and `env[].valueFrom` of every container and init container, and
    every volume, a projected volume's sources included.
    """
    spec = template.get("spec") or {}
    found: set[Ref] = set()
    for container in [
        *(spec.get("containers") or []),
        *(spec.get("initContainers") or []),
    ]:
        for source in container.get("envFrom") or []:
            if (source.get("configMapRef") or {}).get("name"):
                found.add(("ConfigMap", source["configMapRef"]["name"]))
            if (source.get("secretRef") or {}).get("name"):
                found.add(("Secret", source["secretRef"]["name"]))
        for env in container.get("env") or []:
            value_from = env.get("valueFrom") or {}
            if (value_from.get("configMapKeyRef") or {}).get("name"):
                found.add(("ConfigMap", value_from["configMapKeyRef"]["name"]))
            if (value_from.get("secretKeyRef") or {}).get("name"):
                found.add(("Secret", value_from["secretKeyRef"]["name"]))
    for volume in spec.get("volumes") or []:
        sources = [volume] + [
            s for s in (volume.get("projected") or {}).get("sources") or []
        ]
        for source in sources:
            if (source.get("configMap") or {}).get("name"):
                found.add(("ConfigMap", source["configMap"]["name"]))
            if (source.get("secret") or {}).get("secretName"):
                found.add(("Secret", source["secret"]["secretName"]))
            if (source.get("secret") or {}).get("name"):
                found.add(("Secret", source["secret"]["name"]))
    return found


def _documents(files: Iterable[str]) -> list[dict]:
    """Each mapping document of `files`, in order."""
    return [
        obj
        for text in files
        for obj in yaml_fast.safe_load_all(text)
        if isinstance(obj, dict)
    ]


def sops_secrets(sops: Mapping[str, str | None], keys: Iterable[str]) -> dict[str, set]:
    """Each Secret name a `render_sops_manifests` entry renders, mapped to its keys.

    Args:
        sops: Each entry's sentinel render, as `PlaceholderRender.sops_manifests` holds it.
        keys: The key names of `ansible/vars/secrets.yml`.
    """
    sentinels = Sentinels(keys)
    found: dict[str, set] = {}
    for entry, text in sops.items():
        for obj in _documents([text or ""]):
            name = (obj.get("metadata") or {}).get("name")
            if obj.get("kind") == "Secret" and name:
                found.setdefault(name, set()).update(
                    sentinels.key_set({entry: text or ""})
                )
    return found


def restart_values(
    tag: str,
    directories: Mapping[str, Mapping[str, str]],
    targets: Iterable[str],
    keys: Iterable[str],
    source: Mapping[str, str],
    sops: Mapping[str, str | None] | None = None,
) -> dict[str, str]:
    """Each target workload of `tag` that names a ConfigMap or Secret, mapped to its value.

    Args:
        tag: The deploy tag, named in a refusal.
        directories: The tag's placeholder-form directories, each file name to its text.
        targets: The workload names `restart_targets` returns.
        keys: The key names of `ansible/vars/secrets.yml`.
        source: Each key in `ansible/vars/cluster-vars.source` mapped to its digest.
        sops: The tag's `render_sops_manifests` entries mapped to their sentinel renders.

    Raises:
        RenderError: A target names no rendered workload, or a Secret it names references a
            key that `.source` does not record, so a rotation of it would roll nothing.
    """
    keys = frozenset(keys)
    texts = [text for files in directories.values() for text in files.values()]
    docs = _documents(texts)
    own = {
        (obj.get("kind"), (obj.get("metadata") or {}).get("name")): obj for obj in docs
    }
    shipped = sops_secrets(sops or {}, keys)
    workloads = {
        (obj.get("metadata") or {}).get("name"): template
        for obj in docs
        if (template := pod_template(obj)) is not None
    }
    values = {}
    for name in sorted(targets):
        if name not in workloads:
            raise RenderError(
                f"{tag}: restart target {name} is no Deployment, DaemonSet or "
                "StatefulSet in its render"
            )
        inputs = []
        for kind, ref in sorted(pod_references(workloads[name])):
            held = set()
            if (kind, ref) in own:
                canonical = json.dumps(
                    own[(kind, ref)], sort_keys=True, separators=(",", ":")
                )
                inputs.append(f"{kind}/{ref}\0{canonical}")
                if kind == "Secret":
                    held = set(references(canonical)) & keys
            elif kind == "Secret" and ref in shipped:
                inputs.append(f"{kind}/{ref}\0sops-manifest")
                held = shipped[ref]
            for key in sorted(held):
                if key not in source:
                    raise RenderError(
                        f"{tag}: {name} reads Secret {ref}, whose key {key} has no line in "
                        "cluster-vars.source; cluster_vars.py write (operator-only) adds it"
                    )
                inputs.append(f"{key}\0{source[key]}")
        if inputs:
            values[name] = hashlib.sha256("\n".join(inputs).encode()).hexdigest()
    return values


def _child(node: yaml.Node | None, key: str) -> yaml.Node | None:
    """The value node under `key` in mapping `node`, or None."""
    if not isinstance(node, yaml.MappingNode):
        return None
    for k, v in node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return v
    return None


def _insertion(
    lines: list[str], node: yaml.Node | None, block: list[str], where: str
) -> tuple[int, str]:
    """Where to insert `block` as the first entry of mapping `node`, and the text to insert.

    Raises:
        RenderError: `node` is a flow mapping, an empty one, or its first key does not open
            its own line, so no line can be inserted without reformatting the document.
    """
    if not isinstance(node, yaml.MappingNode) or node.flow_style or not node.value:
        raise RenderError(f"{where}: the pod template's mapping is not block style")
    mark = node.value[0][0].start_mark
    if lines[mark.line][: mark.column].strip():
        raise RenderError(f"{where}: the pod template's first key does not open a line")
    indent = " " * mark.column
    return mark.line, "".join(f"{indent}{line}\n" for line in block)


def stamp(text: str, values: Mapping[str, str], where: str) -> str:
    """`text` with `checksum/restart` on the pod template of each workload in `values`.

    Every other byte of `text` is kept. A workload missing from `text` is left alone.

    Raises:
        RenderError: A workload already carries the annotation, or its template is in a
            shape `_insertion` cannot extend.
    """
    if not values:
        return text
    lines = text.splitlines(keepends=True)
    inserts = []
    for doc in yaml.compose_all(text, Loader=yaml.SafeLoader):
        kind = _child(doc, "kind")
        name = _child(_child(doc, "metadata"), "name")
        if not (
            isinstance(kind, yaml.ScalarNode)
            and kind.value in WORKLOAD_KINDS
            and isinstance(name, yaml.ScalarNode)
            and name.value in values
        ):
            continue
        value = f'{RESTART_ANNOTATION}: "{values[name.value]}"'
        template = _child(_child(doc, "spec"), "template")
        metadata = _child(template, "metadata")
        annotations = _child(metadata, "annotations")
        label = f"{where} {kind.value}/{name.value}"
        if metadata is None:
            block = ["metadata:", "  annotations:", f"    {value}"]
            inserts.append(_insertion(lines, template, block, label))
        elif annotations is None:
            block = ["annotations:", f"  {value}"]
            inserts.append(_insertion(lines, metadata, block, label))
        elif _child(annotations, RESTART_ANNOTATION) is not None:
            raise RenderError(f"{label} already carries {RESTART_ANNOTATION}")
        else:
            inserts.append(_insertion(lines, annotations, [value], label))
    for line, block in sorted(inserts, reverse=True):
        lines.insert(line, block)
    return "".join(lines)
