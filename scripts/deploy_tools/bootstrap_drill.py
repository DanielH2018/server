"""Fixture bundle and verdicts for the bootstrap drill (D5 step 28, `bootstrap-drill.yml`).

The drill rehearses the order of the rebuilt-cluster procedure on a throwaway kind cluster: the
OCIRepository first, the plumbing closure next, a stand-in for the Longhorn restores, and the
root Kustomization last. This module builds what the workflow pushes and applies, and judges
what the cluster reports back. It runs no `kubectl`: the workflow pipes `kubectl get -o json`
into the `check` subcommands, so every verdict is testable on fixture JSON.

- `build` writes `bundle/` (the artifact the registry serves) and `hand/` (the documents the
  procedure applies by hand). The root and the OCIRepository come from the renderer's own
  `cluster/flux-deferred/sync.yaml`, patched to the drill's registry without `verify`.
- `check before-root`, `check after-root` and `check no-decrypt` print one problem per line
  and exit 1 when there is any.

Run (the workflow's form): uv run python scripts/deploy_tools/bootstrap_drill.py build \
    --sync out/cluster/flux-deferred/sync.yaml --registry kind-registry:5000 \
    --age-recipient AGE_PUBLIC_KEY --out DIR
"""

import json
import subprocess
import sys
import sys as _sys
from collections.abc import Callable
from pathlib import Path
from pathlib import Path as _Path

import yaml

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK

NAMESPACE = "drill"
PLUMBING = "drill-plumbing"
STATEFUL = "drill-stateful"
CLAIM = "drill-data"
MARKER_CONFIGMAP = "drill-stateful-marker"
MARKER_LABEL = "drill.homelab/restored-by-standin"
OWNER_LABEL = "kustomize.toolkit.fluxcd.io/name"
CLUSTER_VARS_SECRET = "homelab-cluster-vars"
FIXTURE_KEY = "drill_fixture_key"
# The two names the hand-applied closure must never reach: `flux-deferred` applies the root.
FORBIDDEN_IN_CLOSURE = frozenset({"flux-deferred", "homelab"})
ARTIFACT_TAG = "drill"

_KUSTOMIZE = "kustomize.toolkit.fluxcd.io/v1"


def dependson_closure(depends: dict[str, list[str]], start: set[str]) -> set[str]:
    """Return `start` plus every Kustomization its `dependsOn` chains reach."""
    seen: set[str] = set()
    todo = list(start)
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo.extend(depends.get(name, []))
    return seen


def closure_problems(depends: dict[str, list[str]], plumbing: set[str]) -> list[str]:
    """Name each plumbing tag whose closure reaches `flux-deferred` or the root."""
    return [
        f"{tag}: the dependsOn closure reaches {sorted(hit)}"
        for tag in sorted(plumbing)
        if (hit := dependson_closure(depends, {tag}) & FORBIDDEN_IN_CLOSURE)
    ]


def sync_problems(docs: list[dict]) -> list[str]:
    """Name what `cluster/flux-deferred/` lacks of the objects steps 5 and 9 apply."""
    held = {(d.get("kind"), d.get("metadata", {}).get("name")) for d in docs}
    return [
        f"flux-deferred holds no {kind} {name}"
        for kind, name in (("OCIRepository", "homelab"), ("Kustomization", "homelab"))
        if (kind, name) not in held
    ]


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def placeholder_problems(docs: list[dict]) -> list[str]:
    """Name each string of a hand-applied document that holds `${` or `$$`.

    A hand apply runs no substitution, so such a string reaches the API server verbatim.
    """
    return [
        f"{d.get('kind')} {d.get('metadata', {}).get('name')}: {s!r} holds a placeholder"
        for d in docs
        for s in _strings(d)
        if "${" in s or "$$" in s
    ]


def patch_sync(docs: list[dict], registry: str) -> tuple[dict, dict]:
    """Return the drill's OCIRepository and the root, from the rendered sync documents.

    The OCIRepository points at the drill's registry, over plain HTTP and without `verify`.
    The root is the rendered one, untouched.
    """
    pick = {
        (d["kind"], d["metadata"]["name"]): d
        for d in docs
        if d.get("kind") in ("OCIRepository", "Kustomization")
    }
    source = json.loads(json.dumps(pick[("OCIRepository", "homelab")]))
    source["spec"].pop("verify", None)
    source["spec"].update(
        url=f"oci://{registry}/{ARTIFACT_TAG}", insecure=True, ref={"tag": ARTIFACT_TAG}
    )
    return source, pick[("Kustomization", "homelab")]


def _kustomization(name: str, depends: list[str], *, wait: bool = True) -> dict:
    spec = {
        "sourceRef": {"kind": "OCIRepository", "name": "homelab"},
        "path": f"./cluster/{name}",
        "interval": "10m",
        "retryInterval": "30s",
        "timeout": "2m",
        "prune": True,
        "wait": wait,
        "deletionPolicy": "Orphan",
    }
    if depends:
        spec["dependsOn"] = [{"name": n} for n in depends]
    if name == "cluster-vars":
        spec["decryption"] = {"provider": "sops", "secretRef": {"name": "sops-age"}}
    return {
        "apiVersion": _KUSTOMIZE,
        "kind": "Kustomization",
        "metadata": {"name": name, "namespace": "flux-system"},
        "spec": spec,
    }


def _object(kind: str, name: str, api: str = "v1", **extra) -> dict:
    return {
        "apiVersion": api,
        "kind": kind,
        "metadata": {"name": name, "namespace": NAMESPACE},
        **extra,
    }


def sops_encrypt(env_text: str, recipient: str, scratch: Path) -> str:
    """Encrypt a dotenv file to one age recipient with the `sops` on PATH."""
    plain = scratch / "cluster-vars.plain.env"
    plain.write_text(env_text)
    done = subprocess.run(
        ["sops", "--encrypt", "--input-type", "dotenv", "--output-type", "dotenv",
         "--age", recipient, str(plain)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    plain.unlink()
    return done.stdout


def _write_all(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _dump(*docs: dict) -> str:
    return yaml.safe_dump_all(docs, sort_keys=False)


def _kustomize_dir(*resources: str) -> str:
    return _dump(
        {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "resources": list(resources),
        }
    )


def build(
    out: Path,
    sync_docs: list[dict],
    registry: str,
    encrypt: Callable[[str], str],
) -> None:
    """Write `bundle/` and `hand/` under `out`.

    Args:
        out: An absent directory.
        sync_docs: The documents of the rendered `cluster/flux-deferred/sync.yaml`.
        registry: HOST:PORT that source-controller pulls the artifact from.
        encrypt: Turns the cluster-vars dotenv into SOPS ciphertext.
    """
    children = {
        "cluster-vars": _kustomization("cluster-vars", []),
        PLUMBING: _kustomization(PLUMBING, ["cluster-vars"]),
        # A claim on kind's default StorageClass stays Pending until a pod mounts it, so the
        # Kustomization cannot wait on its health.
        STATEFUL: _kustomization(STATEFUL, [PLUMBING], wait=False),
    }
    claim = _object(
        "PersistentVolumeClaim",
        CLAIM,
        spec={
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "64Mi"}},
        },
    )
    files = {f"flux/{n}.yaml": _dump(k) for n, k in children.items()}
    files["flux/kustomization.yaml"] = _kustomize_dir(*(f"{n}.yaml" for n in children))
    files["cluster/cluster-vars/cluster-vars.sops.env"] = encrypt(f"{FIXTURE_KEY}=on\n")
    files["cluster/cluster-vars/kustomization.yaml"] = _dump(
        {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "namespace": "flux-system",
            "generatorOptions": {"disableNameSuffixHash": True},
            "secretGenerator": [
                {"name": CLUSTER_VARS_SECRET, "envs": ["cluster-vars.sops.env"]}
            ],
        }
    )
    files[f"cluster/{PLUMBING}/namespace.yaml"] = _dump(
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}}
    )
    files[f"cluster/{PLUMBING}/kustomization.yaml"] = _kustomize_dir("namespace.yaml")
    files[f"cluster/{STATEFUL}/claim.yaml"] = _dump(claim)
    files[f"cluster/{STATEFUL}/marker.yaml"] = _dump(
        _object("ConfigMap", MARKER_CONFIGMAP)
    )
    files[f"cluster/{STATEFUL}/kustomization.yaml"] = _kustomize_dir(
        "claim.yaml", "marker.yaml"
    )
    _write_all(out / "bundle", files)

    source, root = patch_sync(sync_docs, registry)
    standin = json.loads(json.dumps(claim))
    standin["metadata"]["labels"] = {MARKER_LABEL: "true"}
    depends = {
        n: [d["name"] for d in k["spec"].get("dependsOn", [])]
        for n, k in children.items()
    }
    step6 = sorted(dependson_closure(depends, {PLUMBING}))
    _write_all(
        out / "hand",
        {
            "ocirepository.yaml": _dump(source),
            "root.yaml": _dump(root),
            "standin-claim.yaml": _dump(standin),
            "step6.txt": "".join(f"{out}/bundle/flux/{n}.yaml\n" for n in step6),
        },
    )


def _find(items: list[dict], kind: str, name: str, namespace: str) -> dict | None:
    for item in items:
        meta = item.get("metadata", {})
        if (item.get("kind"), meta.get("name"), meta.get("namespace")) == (
            kind,
            name,
            namespace,
        ):
            return item
    return None


def _ready(obj: dict | None) -> str | None:
    for cond in (obj or {}).get("status", {}).get("conditions", []):
        if cond.get("type") == "Ready":
            return cond.get("status")
    return None


def before_root_problems(items: list[dict]) -> list[str]:
    """Judge the cluster after the stand-in and before the root exists (step 8).

    Only the stand-in has written the claim, so the stateful tag has no Kustomization and no
    ConfigMap, and the claim carries the marker label and no ownership label.
    """
    problems = []
    if _find(items, "Kustomization", STATEFUL, "flux-system"):
        problems.append(f"Kustomization {STATEFUL} exists before the root was applied")
    if _find(items, "ConfigMap", MARKER_CONFIGMAP, NAMESPACE):
        problems.append(
            f"ConfigMap {MARKER_CONFIGMAP} exists before the root was applied"
        )
    labels = (
        (_find(items, "PersistentVolumeClaim", CLAIM, NAMESPACE) or {})
        .get("metadata", {})
        .get("labels", {})
    )
    if MARKER_LABEL not in labels:
        problems.append(f"claim {CLAIM} lacks the stand-in's marker label")
    if OWNER_LABEL in labels:
        problems.append(
            f"claim {CLAIM} already carries {OWNER_LABEL}, so Flux created it"
        )
    return problems


def after_root_problems(items: list[dict], standin_uid: str) -> list[str]:
    """Judge the cluster after the root has applied everything (step 9)."""
    problems = []
    if _ready(_find(items, "Kustomization", STATEFUL, "flux-system")) != "True":
        problems.append(f"Kustomization {STATEFUL} is not Ready")
    if not _find(items, "ConfigMap", MARKER_CONFIGMAP, NAMESPACE):
        problems.append(f"ConfigMap {MARKER_CONFIGMAP} does not exist")
    claim = _find(items, "PersistentVolumeClaim", CLAIM, NAMESPACE) or {}
    meta = claim.get("metadata", {})
    if MARKER_LABEL not in meta.get("labels", {}):
        problems.append(f"claim {CLAIM} lost the stand-in's marker label")
    if meta.get("uid") != standin_uid:
        problems.append(f"claim {CLAIM} is not the stand-in's object (uid changed)")
    if meta.get("labels", {}).get(OWNER_LABEL) != STATEFUL:
        problems.append(f"claim {CLAIM} is not owned by Kustomization {STATEFUL}")
    plumbing = _find(items, "Kustomization", PLUMBING, "flux-system")
    if _ready(plumbing) != "True":
        problems.append(f"Kustomization {PLUMBING} is not Ready")
    if (plumbing or {}).get("metadata", {}).get("labels", {}).get(
        OWNER_LABEL
    ) != "homelab":
        problems.append(
            f"Kustomization {PLUMBING} does not carry the root's ownership labels"
        )
    secret = _find(items, "Secret", CLUSTER_VARS_SECRET, "flux-system")
    if FIXTURE_KEY not in (secret or {}).get("data", {}):
        problems.append(f"{CLUSTER_VARS_SECRET} holds no {FIXTURE_KEY}")
    return problems


def no_decrypt_problems(items: list[dict]) -> list[str]:
    """Judge the leg that skips `sops-age`: `cluster-vars` must fail on decryption."""
    ks = _find(items, "Kustomization", "cluster-vars", "flux-system")
    if _ready(ks) == "True":
        return ["cluster-vars is Ready without the sops-age Secret"]
    conds = (ks or {}).get("status", {}).get("conditions", [])
    message = " ".join(c.get("message", "") for c in conds if c.get("type") == "Ready")
    if "decrypt" not in message.lower() and "sops-age" not in message:
        return [f"cluster-vars failed, but not on decryption: {message[:200]!r}"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser(
        "build", help="write the fixture bundle and the hand-applied files"
    )
    b.add_argument(
        "--sync", type=Path, required=True, help="rendered flux-deferred/sync.yaml"
    )
    b.add_argument(
        "--registry", required=True, help="HOST:PORT source-controller pulls from"
    )
    b.add_argument(
        "--age-recipient", required=True, help="the throwaway age public key"
    )
    b.add_argument("--out", type=Path, required=True, help="an absent directory")
    c = sub.add_parser("check", help="judge `kubectl get ... -o json` read from stdin")
    c.add_argument("verdict", choices=["before-root", "after-root", "no-decrypt"])
    c.add_argument(
        "--standin-uid", default="", help="after-root: the stand-in claim's uid"
    )
    args = parser.parse_args(argv)
    if args.command == "build":
        docs = [d for d in yaml_fast.safe_load_all(args.sync.read_text()) if d]
        args.out.mkdir(parents=True)
        build(args.out, docs, args.registry,
              lambda text: sops_encrypt(text, args.age_recipient, args.out))  # fmt: skip
        print(f"built {args.out}")
        return OK
    items = json.load(sys.stdin)["items"]
    problems = {
        "before-root": lambda: before_root_problems(items),
        "after-root": lambda: after_root_problems(items, args.standin_uid),
        "no-decrypt": lambda: no_decrypt_problems(items),
    }[args.verdict]()
    print("\n".join(problems) if problems else f"{args.verdict}: ok")
    return FAILED if problems else OK


if __name__ == "__main__":
    sys.exit(main())
