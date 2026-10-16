"""`checksum/restart`: the pod annotation that rolls a flux-owned workload (target slice D3.3c).

Ansible's shared `rollout restart` does not exist under Flux, so the renderer stamps
`checksum/restart` on the pod template of each restart target of a flux-owned tag, and the
pod rolls when the value moves. The targets are those the shared `k8s/manifests` role
restarts on a render change: the `manifests_rollout` workload when it is non-empty, and each
`manifests_extra_rollouts` entry whose `restart_on` names `config` or `secret`
(`restart_targets`). An entry's `restart_on` also narrows its inputs, as it narrows Ansible's
restart: `config` admits its ConfigMaps and `secret` its Secrets.

Every `manifests_self_rollouts` entry is a target too (D5.1e): pihole and observability
restart those workloads in their own tasks, which a flux-owned tag never runs. Such an entry
admits both kinds whatever its `restart_on` says, because there `restart_on` describes the
Ansible release record (observability declares `[]` on every entry), and `rolled_by_role`
does not exempt it.

A target's value is one SHA-256 over:

- each ConfigMap and Secret its pod template names through `envFrom`, `env[].valueFrom` or
  `volumes`, from the tag's own placeholder-form render, re-serialised in canonical form, so a
  YAML comment edit moves nothing;
- the `.source` digest of every secret key those ConfigMaps and Secrets reference as
  `${key}`, because the placeholder text does not move when the key rotates;
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

import ast
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
from renderer.renderer_lib.environment import RenderError, evaluate, renderer_env
from renderer.renderer_lib.placeholders import Sentinels, references
from renderer.renderer_lib.task_inputs import include_vars

RESTART_ANNOTATION = "checksum/restart"
# The kinds `kubectl rollout restart` acts on, the only kinds the shared role restarts.
WORKLOAD_KINDS = frozenset({"Deployment", "DaemonSet", "StatefulSet"})
TARGET_KEYS = ("manifests_rollout", "manifests_extra_rollouts")
SELF_KEY = "manifests_self_rollouts"
# `manifests_restart_triggers_default` in the manifests role's defaults: the signals an entry
# restarts on when it names no `restart_on`. Only `config` and `secret` move with the render;
# `image` is a rebuilt image behind a mutable tag, which Flux sees in the render already.
DEFAULT_TRIGGERS = frozenset({"config", "secret", "image"})
# Each signal to the kind of input it admits into the value.
TRIGGER_KINDS = {"config": "ConfigMap", "secret": "Secret"}

Ref = tuple[str, str]


def restart_targets(
    tag: str, context: Mapping, roles: Path = K8S_ROLES
) -> dict[str, frozenset[str]]:
    """Each workload the shared role restarts for `tag`, mapped to the kinds that restart it.

    The kinds are `ConfigMap` and `Secret`, as the target's `restart_on` admits them. An
    extra-rollouts entry whose `restart_on` names neither, such as `[]`, is waited on and never
    restarted by Ansible, so it is no target here (scrutiny's influxdb and collector,
    authelia's redis).

    A `manifests_self_rollouts` entry admits both kinds, whatever its `restart_on` names
    (D5.1e).

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
    extra = _entries(tag, "manifests_extra_rollouts", values)
    if not isinstance(primary, str):
        raise RenderError(f"{tag}: manifests_rollout must be a name")
    targets = {primary: _kinds(DEFAULT_TRIGGERS)} if primary else {}
    for entry in extra:
        kinds = _kinds(entry.get("restart_on", DEFAULT_TRIGGERS))
        if kinds:
            targets[str(entry["name"])] = kinds
    for name in self_rollouts(tag, context, roles):
        targets[name] = frozenset(TRIGGER_KINDS.values())
    return dict(sorted(targets.items()))


def self_rollouts(tag: str, context: Mapping, roles: Path = K8S_ROLES) -> list[str]:
    """The workload names of `tag`'s `manifests_self_rollouts`, empty when it passes none.

    Args:
        tag: The deploy tag, whose role is `roles/<tag>`.
        context: The play context the include's Jinja values resolve against.
        roles: The k8s roles directory; a test passes a fixture tree.

    Raises:
        RenderError: The value does not resolve, is not a list, or an entry has no name.
    """
    value = include_vars(roles / tag).get(SELF_KEY)
    if isinstance(value, str):
        # observability builds its list with `combine`, an Ansible filter `resolve_vars`
        # lacks, so the value is evaluated as `derived_facts` evaluates a task expression.
        env = renderer_env(roles / tag / "templates")
        try:
            value = evaluate(env, value.strip(), dict(context))
        except jinja2.TemplateError as exc:
            raise RenderError(f"{tag}: {SELF_KEY} does not resolve: {exc}") from exc
    return [str(e["name"]) for e in _entries(tag, SELF_KEY, {SELF_KEY: value})]


def _entries(tag: str, key: str, values: Mapping) -> list[dict]:
    """The `{name: ...}` entries of the list `key` in `values`, empty when it is unset.

    Raises:
        RenderError: The value is not a list, or an entry has no name.
    """
    entries = values.get(key) or []
    if isinstance(entries, str):
        # An expression that builds the list inline, as authelia's does, renders as its
        # text; Ansible turns such text back into the list, and so does this.
        try:
            entries = ast.literal_eval(entries.strip() or "[]")
        except ValueError, SyntaxError:
            pass
    if not isinstance(entries, list):
        raise RenderError(f"{tag}: {key} must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise RenderError(f"{tag}: a {key} entry has no name: {entry!r}")
    return entries


def _kinds(triggers: Iterable[str]) -> frozenset[str]:
    """The input kinds `triggers`, a `restart_on` list, admits into a value."""
    return frozenset(TRIGGER_KINDS[t] for t in triggers if t in TRIGGER_KINDS)


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
    targets: Mapping[str, Iterable[str]],
    keys: Iterable[str],
    source: Mapping[str, str],
    sops: Mapping[str, str | None] | None = None,
) -> dict[str, str]:
    """Each target workload of `tag` that names a ConfigMap or Secret, mapped to its value.

    Args:
        tag: The deploy tag, named in a refusal.
        directories: The tag's placeholder-form directories, each file name to its text.
        targets: Each workload name mapped to the input kinds it admits, as
            `restart_targets` returns them.
        keys: The key names of `ansible/vars/secrets.yml`.
        source: Each key in `ansible/vars/cluster-vars.source` mapped to its digest.
        sops: The tag's `render_sops_manifests` entries mapped to their sentinel renders.

    Raises:
        RenderError: A target names no rendered workload, or a ConfigMap or Secret it names
            references a key that `.source` does not record, so a rotation of it would roll
            nothing.
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
        admitted = frozenset(targets[name])
        for kind, ref in sorted(pod_references(workloads[name])):
            if kind not in admitted:
                continue
            held = set()
            if (kind, ref) in own:
                canonical = json.dumps(
                    own[(kind, ref)], sort_keys=True, separators=(",", ":")
                )
                inputs.append(f"{kind}/{ref}\0{canonical}")
                # A ConfigMap can hold a secret placeholder too, as traefik's static config
                # holds `${email}`; its text stays put when the key rotates.
                held = set(references(canonical)) & keys
            elif kind == "Secret" and ref in shipped:
                inputs.append(f"{kind}/{ref}\0sops-manifest")
                held = shipped[ref]
            for key in sorted(held):
                if key not in source:
                    raise RenderError(
                        f"{tag}: {name} reads {kind} {ref}, whose key {key} has no line in "
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
        raise RenderError(f"{where}: the mapping to extend is not block style")
    mark = node.value[0][0].start_mark
    if lines[mark.line][: mark.column].strip():
        raise RenderError(f"{where}: the mapping's first key does not open a line")
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
