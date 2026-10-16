"""`probe.py takeover <tag>` — the fields Flux's first apply of a tag strips (D5.1g).

kustomize-controller runs a cleanup on every live object before each apply. The cleanup hands
the fields of four managers to kustomize-controller's own Apply entry, and the apply that
follows removes each such field the render does not declare. A server-side dry run never runs
the cleanup, so no dry-run diff shows the strip. speedtest is the case: its `kubectl-rollout`
entry owns `restartedAt`, the takeover drops it, and the pod rolls once.

This census renders the tag in placeholder mode, reads each live object the render names with
`--show-managed-fields` under the read-only ServiceAccount, and prints every leaf field path
that a cleaned manager owns, that no other manager co-owns, and that the render does not
declare. A placeholder sits at the same path as the value it stands for, so the placeholder
render answers whether a path is declared. It prints paths and manager names only: `fieldsV1`
names fields and never values.

What it cannot see is the operator's: an apply that fails on an immutable field, or one that
changes a value. `docs/flux-deploy-plane.md` (*What a takeover strips*) has the operator's
`flux diff kustomization` command for those.

The pure half (`CLEANUP_MANAGERS`, `is_cleaned`, `leaf_paths`, `declares`, `stripped_paths`)
reads no cluster and runs no render, so H1.1j's operator-run `scripts/dev/takeover_census.py`
can import it rather than copy it.

Exit codes: 0 the census printed; 75 the render or a live read failed, or the tag rendered no
object whose live copy could be compared, because an empty census is not a clean one.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import json
import subprocess
import tempfile
from pathlib import Path

from diagnostics.probe_lib.core import Unreadable
from lib import yaml_fast
from lib.kubectl import DEFAULT_CLUSTER, MissingKubectl, kubectl, kubectl_argv
from lib.repo_paths import SCRIPTS

# kustomize-controller v1.9.6, `internal/controller/kustomization_controller.go`: the
# `fieldManagers` list it passes as `applyOpts.Cleanup.FieldManagers`. No entry sets
# `ExactMatch`, so fluxcd/pkg `ssa/patch.go:matchFieldManager` matches each by name PREFIX,
# and only for the same operation. `kubectl-rollout`, `kubectl-patch` and
# `kubectl-client-side-apply` all match `kubectl`. The live kustomize-controller passes no
# `--disallowed-field-manager` and no `--controller-name`, so the list is these four.
CLEANUP_MANAGERS = (
    ("kubectl", "Apply"),
    ("kubectl", "Update"),
    ("before-first-apply", "Update"),
    ("kustomize-controller", "Update"),
)

# The cleanup removes this annotation by name on every object, and no render carries it, so
# listing it would add one expected line per object. D5's exit check reads that it is gone.
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"
LAST_APPLIED_PATH = (("f", "metadata"), ("f", "annotations"), ("f", LAST_APPLIED))

# A removed field under the pod template changes the template, so the workload rolls.
ROLLING_KINDS = frozenset({"Deployment", "DaemonSet", "StatefulSet"})
POD_TEMPLATE = (("f", "spec"), ("f", "template"))

RENDERER = SCRIPTS / "renderer" / "homelab_render.py"


def is_cleaned(entry):
    """True when the cleanup hands this `managedFields` entry to kustomize-controller.

    Mirrors `matchFieldManager`: an entry on a subresource never matches, so a `kubectl scale`
    entry on `scale` or `k3s` on `status` keeps its fields.
    """
    if entry.get("subresource"):
        return False
    manager = entry.get("manager") or ""
    operation = entry.get("operation") or ""
    return any(
        operation == op and manager.startswith(name) for name, op in CLEANUP_MANAGERS
    )


def _segment(key):
    """One `fieldsV1` key as `(kind, value)`: `f` field, `k` keyed item, `v` set value, `i` index.

    A `k` or `v` value stays the JSON text the key carries, so a path stays hashable.
    """
    prefix, _, rest = key.partition(":")
    if prefix in ("k", "v"):
        return (prefix, rest)
    if prefix == "i":
        return ("i", int(rest))
    return ("f", rest)


def leaf_paths(fields, prefix=()):
    """Every leaf path in a `fieldsV1` tree, as tuples of segments.

    The `.` key marks ownership of the node itself and is dropped. A node whose only child is
    `.` is a leaf, such as an empty map the manager created.
    """
    children = {k: v for k, v in (fields or {}).items() if k != "."}
    if not children:
        return {prefix} if prefix else set()
    paths = set()
    for key, sub in children.items():
        paths |= leaf_paths(sub, prefix + (_segment(key),))
    return paths


def _keyed_item(items, keys):
    """The list item a `k:` segment names, or None.

    A key field the render omits counts as matching, because the API server defaults it: a
    render's `containerPort: 80` is the live `{"containerPort":80,"protocol":"TCP"}`. At least
    one key field must be present and equal, so an item with none of them matches nothing.
    """
    for item in items:
        if not isinstance(item, dict):
            continue
        present = [k for k in keys if k in item]
        if present and all(item[k] == keys[k] for k in present):
            return item
    return None


def _lookup(obj, path):
    """`(True, value)` for the field at `path` in `obj`, or `(False, None)` when it is absent."""
    node = obj
    for kind, value in path:
        if kind == "f":
            if not isinstance(node, dict) or value not in node:
                return False, None
            node = node[value]
        elif kind == "k":
            if not isinstance(node, list):
                return False, None
            node = _keyed_item(node, json.loads(value))
            if node is None:
                return False, None
        elif kind == "v":
            item = json.loads(value)
            return (isinstance(node, list) and item in node), item
        else:
            if not isinstance(node, list) or value >= len(node):
                return False, None
            node = node[value]
    return True, node


def declares(obj, path):
    """True when the rendered object `obj` declares the field at `path`."""
    return _lookup(obj, path)[0]


def stripped_paths(live, rendered):
    """`{path: [cleaned managers]}` for each field the takeover removes from `live`.

    A path is stripped when a cleaned manager owns it, no other manager owns it too, and the
    render does not declare it. A co-owner keeps the field live after kustomize-controller drops
    its claim, as server-side apply removes a field only when no manager still owns it.
    """
    cleaned, kept = {}, set()
    for entry in (live.get("metadata") or {}).get("managedFields") or []:
        paths = leaf_paths(entry.get("fieldsV1"))
        if is_cleaned(entry):
            for path in paths:
                cleaned.setdefault(path, []).append(entry.get("manager"))
        else:
            kept |= paths
    return {
        path: sorted(set(managers))
        for path, managers in cleaned.items()
        if path not in kept
        and path != LAST_APPLIED_PATH
        and not declares(rendered, path)
    }


CLIENT_SIDE_APPLY = "kubectl-client-side-apply"


def implied_pull_policy(image):
    """The `imagePullPolicy` the API server defaults for `image`.

    Mirrors `SetDefaults_Container`: `Always` when the reference's tag is `latest`, which a
    reference with neither a tag nor a digest also means, and `IfNotPresent` otherwise.
    """
    name, _, digest = (image or "").partition("@")
    last = name.rsplit("/", 1)[-1]
    tag = last.partition(":")[2] if ":" in last else ("" if digest else "latest")
    return "Always" if tag == "latest" else "IfNotPresent"


def _redefaults_alike(path, live, rendered):
    """False when the server would default this path to a different value than it holds.

    `imagePullPolicy` is the one default that depends on another field: the image reference
    the server read at the create. When the render's reference implies another policy, the
    takeover changes the value and the pod rolls, so the path stays listed.
    """
    if path[-1] != ("f", "imagePullPolicy"):
        return True
    found, image = _lookup(rendered, path[:-1] + (("f", "image"),))
    held = _lookup(live, path)[1]
    return found and held == implied_pull_policy(image)


def server_defaulted(stripped, live, rendered):
    """The stripped paths the API server defaulted when `kubectl apply` created the object.

    A create attributes every field the server defaults, such as `dnsPolicy` or a probe's
    `timeoutSeconds`, to the manager that sent it. So `kubectl-client-side-apply` owns fields
    its own applied config never held, and the object's last-applied annotation is that config.
    A path only it owns and that config omits is a default: the takeover removes it and the
    server defaults it again, to the same value unless `_redefaults_alike` says otherwise. An
    object without the annotation has none, so every path stays in the main list. This rule is
    read from how server-side apply and defaulting work, not measured by an apply, and a field
    set at create by admission or a create strategy also matches it. So `census` prints these
    paths in their own list rather than dropping them.
    """
    annotations = (live.get("metadata") or {}).get("annotations") or {}
    try:
        applied = json.loads(annotations.get(LAST_APPLIED) or "null")
    except json.JSONDecodeError:
        applied = None
    if not isinstance(applied, dict):
        return set()
    return {
        path
        for path, managers in stripped.items()
        if managers == [CLIENT_SIDE_APPLY]
        and not declares(applied, path)
        and _redefaults_alike(path, live, rendered)
    }


def format_path(path):
    """A path in the plan's dotted form: `spec.template.metadata.annotations.<key>`."""
    parts = []
    for kind, value in path:
        if kind == "f":
            parts.append(value)
        elif kind == "k":
            keys = json.loads(value)
            selector = ",".join(f"{k}={v}" for k, v in sorted(keys.items()))
            parts[-1] = f"{parts[-1]}[{selector}]"
        elif kind == "v":
            parts[-1] = f"{parts[-1]}[={value}]"
        else:
            parts[-1] = f"{parts[-1]}[{value}]"
    return ".".join(parts)


def rolls(kind, paths):
    """True when a stripped path sits under a workload's pod template."""
    return kind in ROLLING_KINDS and any(
        path[: len(POD_TEMPLATE)] == POD_TEMPLATE for path in paths
    )


def resource_arg(doc):
    """The `kubectl get` resource for a rendered document, pinned to its group and version."""
    group, _, version = (doc.get("apiVersion") or "").rpartition("/")
    kind = (doc.get("kind") or "").lower()
    return f"{kind}.{version}.{group}" if group else kind


def get_args(doc):
    """The read-only `kubectl get` for the live copy of one rendered document."""
    meta = doc.get("metadata") or {}
    args = ["get", resource_arg(doc), meta.get("name", "")]
    if meta.get("namespace"):
        args += ["-n", meta["namespace"]]
    return args + ["-o", "json", "--show-managed-fields", "--ignore-not-found"]


def render_tag(tag, out):
    """The tag's placeholder-render documents, from its directory and its deferred one."""
    proc = subprocess.run(
        [_sys.executable, str(RENDERER), "cluster", "--out", str(out), "--role", tag],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise Unreadable(
            f"takeover: the render of {tag} exited {proc.returncode}:\n{proc.stderr.strip()}"
        )
    docs = []
    for directory in (out / "cluster" / tag, out / "cluster" / f"{tag}-deferred"):
        for path in sorted(directory.glob("*.yaml")) if directory.is_dir() else ():
            if path.name == "kustomization.yaml":
                continue
            docs += [d for d in yaml_fast.safe_load_all(path.read_text()) if d]
    return docs


class ReadForbidden(Exception):
    """The read-only ServiceAccount may not read this kind, such as a ClusterRole."""


def read_live(cluster, doc):
    """The live object with its managed fields, or None when it does not exist.

    Raises:
        ReadForbidden: RBAC refused the read, which the census reports for that object alone.
        Unreadable: any other failed read, which stops the census.
    """
    try:
        proc = kubectl(cluster, *get_args(doc))
    except MissingKubectl as exc:
        raise Unreadable(f"takeover: {exc}") from exc
    if proc.returncode != 0 and "(Forbidden)" in proc.stderr:
        raise ReadForbidden(proc.stderr.strip())
    if proc.returncode != 0:
        raise Unreadable(
            f"takeover: kubectl {' '.join(get_args(doc))} exited {proc.returncode}:\n"
            f"{proc.stderr.strip()}"
        )
    if not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Unreadable(
            f"takeover: kubectl returned no JSON for {doc_name(doc)}"
        ) from exc


def doc_name(doc):
    """`<Kind> <namespace>/<name>` for a rendered document, without a namespace when it has none."""
    meta = doc.get("metadata") or {}
    ns = f"{meta['namespace']}/" if meta.get("namespace") else ""
    return f"{doc.get('kind')} {ns}{meta.get('name')}"


def census(tag, docs, live_of):
    """The census text and the number of objects compared, for `docs` and a live reader."""
    lines = [f"takeover census for {tag}: the fields Flux's first apply strips"]
    compared = stripped_total = defaulted_total = 0
    rolling = []
    unchecked = []
    for doc in docs:
        name = doc_name(doc)
        if doc.get("kind") == "Secret":
            lines.append(f"  {name}: skipped, D3.5 hands Secret fields over first")
            continue
        try:
            live = live_of(doc)
        except ReadForbidden:
            # DECIDED: route an unreadable object to the operator rather than widen the
            # read-only ServiceAccount. Its ClusterRole leaves `rbac.*` out on purpose
            # (roles/setup/k3s/templates/readonly-rbac.yaml.j2, *Deliberately absent*), and
            # `view` omits Roles too, so every RBAC object a tag renders lands here (#5258).
            lines.append(
                f"  {name}: not readable by the read-only ServiceAccount, so not checked"
            )
            unchecked.append(name)
            continue
        if live is None:
            lines.append(f"  {name}: not live, so the takeover creates it")
            continue
        compared += 1
        stripped = stripped_paths(live, doc)
        defaulted = server_defaulted(stripped, live, doc)
        stripped = {p: m for p, m in stripped.items() if p not in defaulted}
        if not stripped and not defaulted:
            lines.append(f"  {name}: strips nothing")
            continue
        flag = rolls(doc.get("kind"), stripped)
        lines.append(f"  {name}:{' rolls' if flag else ''}")
        if flag:
            rolling.append(name)
        for path in sorted(stripped, key=format_path):
            lines.append(f"    {format_path(path)}  ({', '.join(stripped[path])})")
        if defaulted:
            # Listed apart, never dropped: the re-default is inferred, and a field set at
            # create by admission or a strategy rather than by defaulting is not restored.
            lines.append(
                f"    presumed server defaults, inferred and not measured ({len(defaulted)}):"
            )
            for path in sorted(defaulted, key=format_path):
                lines.append(f"      {format_path(path)}")
        stripped_total += len(stripped)
        defaulted_total += len(defaulted)
    lines.append(
        f"{stripped_total} field path(s) stripped and {defaulted_total} presumed server "
        f"default(s) across {compared} live object(s); "
        f"{len(rolling)} workload(s) roll"
        + (f": {', '.join(rolling)}" if rolling else "")
    )
    if unchecked:
        lines.append(
            f"{len(unchecked)} object(s) not checked; the operator runs "
            "scripts/dev/takeover_census.py (H1.1j) with the admin kubeconfig over them: "
            + ", ".join(unchecked)
        )
    return "\n".join(lines), compared


def run_takeover(ns, *, render=render_tag, read=read_live):
    """Print the takeover census for `ns.tag`."""
    cluster = getattr(ns, "cluster", DEFAULT_CLUSTER)
    with tempfile.TemporaryDirectory(prefix="takeover-") as tmp:
        docs = render(ns.tag, Path(tmp))
    if getattr(ns, "dry_run", False):
        for doc in docs:
            if doc.get("kind") != "Secret":
                print(" ".join(kubectl_argv(*get_args(doc))))
        return 0
    text, compared = census(ns.tag, docs, lambda doc: read(cluster, doc))
    print(text)
    if not compared:
        raise Unreadable(
            f"takeover: no object {ns.tag} renders is live to compare, so the census proves "
            "nothing. Check the tag name and that the service is deployed."
        )
    return 0
