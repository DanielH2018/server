#!/usr/bin/env python3
"""The fresh-cluster case of the Flux deletion drill (`flux-deletion-drill.yml`, #7555).

Break-glass promotion onto a rebuilt cluster that can reach neither GHCR nor a registry
(`docs/break-glass.md`, *A fresh or rebuilt cluster*). The workflow runs it on a second kind
cluster whose node cannot resolve any name: the Flux controllers and the registry come from
image archives, and the in-cluster registry is the registry role's own render. This module
writes what the workflow applies and judges what the cluster reports, as `deletion_drill.py`
does for the other cases.

- `build` writes `kind.yaml` (the cluster's CIDRs, so the Service's pinned ClusterIP is
  valid), `images.json` (each image the case loads), `prereq/`, `registry/`, `bundle/`,
  `hand/` and `push.json`.
- `archive` names a crane OCI layout for containerd and tars it, as the kit holds an image.
- `check` prints one `case=fresh-cluster verdict=<pass|fail>` line, then one problem per line.

Run (the workflow's form):
    uv run python scripts/deploy_tools/fresh_cluster_drill.py build --bg-pubkey bg.pub --out DIR
"""

import json
import re
import sys
import sys as _sys
import tarfile
from pathlib import Path
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

import yaml
from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK
from lib.repo_paths import ALL_VARS, K8S_ROLES
from renderer.deletion_drill import (
    FLUX_DEFAULTS,
    FLUX_NAMESPACE,
    ROOT,
    _artifact_revision,
    _condition,
    _find,
    _render_flux,
    _write_all,
    bundle,
    drill_image,
    production_root,
)
from renderer.homelab_render import secret_keys
from renderer.renderer_lib.cluster import render_tag
from renderer.renderer_lib.namespace import namespace_documents
from renderer.renderer_lib.placeholders import placeholder_vars

CASE = "fresh-cluster"
FRESH = "fresh-drill"
FRESH_MARKER = "fresh-marker"
COMPONENTS = K8S_ROLES / "flux" / "files" / "gotk-components.yaml"
REGISTRY_DEFAULTS = K8S_ROLES / "registry" / "defaults" / "main.yml"
# kind's own provisioner, preloaded in its node image, stands in for Longhorn.
STAND_IN_PROVISIONER = "rancher.io/local-path"
# The annotation containerd's importer takes verbatim as the image's name.
CONTAINERD_NAME = "io.containerd.image.name"
IMAGE_LINE = re.compile(r"^\s+image: (\S+)$", re.MULTILINE)


# --- The images the kit holds ---------------------------------------------------------


def kit_images() -> list[str]:
    """The three controller images `gotk-components.yaml` pins, then the registry's image."""
    controllers = IMAGE_LINE.findall(COMPONENTS.read_text())
    registry = yaml_fast.safe_load(REGISTRY_DEFAULTS.read_text())["registry_k8s_image"]
    return [*controllers, registry]


def containerd_name(ref: str) -> str:
    """The name the kubelet looks `ref` up by: the normalized repository at its digest.

    CRI drops the tag of a `repo:tag@digest` reference and adds `docker.io/library/` to a
    bare Docker Hub name, so an image stored under any other name is pulled again.

    Raises:
        ValueError: `ref` is not pinned by a sha256 digest.
    """
    name, _, digest = ref.partition("@")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(f"{ref} is not pinned by digest")
    if ":" in name.rsplit("/", 1)[-1]:
        name = name.rsplit(":", 1)[0]
    first = name.split("/", 1)[0]
    if "/" not in name or not ("." in first or ":" in first or first == "localhost"):
        name = f"docker.io/{name if '/' in name else 'library/' + name}"
    return f"{name}@{digest}"


def name_layout(layout: Path, ref: str) -> None:
    """Name the one manifest of `layout`'s `index.json` for containerd, after checking its digest.

    A layout `crane pull --format=oci` writes for an index-pinned reference holds the whole
    index, so its one entry carries the pinned digest. Anything else would import under a
    digest no Deployment names.

    Raises:
        ValueError: The layout holds more or fewer than one manifest, or not the pinned one.
    """
    path = layout / "index.json"
    index = json.loads(path.read_text())
    digest = ref.partition("@")[2]
    served = [m["digest"] for m in index.get("manifests", [])]
    if served != [digest]:
        raise ValueError(f"{layout} holds {served}, not the pinned {digest}")
    entry = index["manifests"][0]
    entry["annotations"] = {
        **entry.get("annotations", {}),
        CONTAINERD_NAME: containerd_name(ref),
    }
    path.write_text(json.dumps(index))


def archive(layout: Path, ref: str, out: Path) -> None:
    """Name `layout` for `ref` and write it as the tar `ctr images import` reads."""
    name_layout(layout, ref)
    with tarfile.open(out, "w") as tar:
        for path in sorted(layout.rglob("*")):
            tar.add(path, arcname=str(path.relative_to(layout)), recursive=False)


# --- What the case applies ------------------------------------------------------------


def kind_config() -> dict:
    """A kind cluster on the k3s CIDRs, so the registry's pinned ClusterIP is in range."""
    found = yaml_fast.safe_load(ALL_VARS.read_text())
    return {
        "kind": "Cluster",
        "apiVersion": "kind.x-k8s.io/v1alpha4",
        "networking": {
            "podSubnet": found["k3s_pod_cidr"],
            "serviceSubnet": found["k3s_service_cidr"],
        },
    }


def _stubs() -> dict:
    return placeholder_vars(secret_keys())


def registry_files() -> dict[str, str]:
    """The registry role's rendered directory, with each pod's node pin dropped.

    The pin names a production host, which no kind node is. Nothing else changes, so the
    case applies what the runbook applies.
    """
    files = {}
    for name, text in render_tag("registry", _stubs()).files.items():
        docs = [d for d in yaml_fast.safe_load_all(text) if d]
        pinned = False
        for doc in docs:
            pod = doc.get("spec", {}).get("template", {}).get("spec", {})
            pinned |= pod.pop("nodeSelector", None) is not None
        files[name] = yaml.safe_dump_all(docs, sort_keys=False) if pinned else text
    return files


def claim_classes(files: dict[str, str]) -> set[str]:
    """Every StorageClass a claim in `files` names."""
    return {
        doc["spec"]["storageClassName"]
        for text in files.values()
        for doc in yaml_fast.safe_load_all(text)
        if doc and doc.get("kind") == "PersistentVolumeClaim"
    }


def prereq_files(classes: set[str]) -> dict[str, str]:
    """What the registry's pod needs before it schedules.

    The `homelab` Namespace and the PriorityClasses come from their own renders, as on a
    rebuild. Each class in `classes` is a stand-in on kind's provisioner, which only the
    drill uses.
    """
    files = {
        name: yaml.safe_dump(doc, sort_keys=False)
        for name, doc in namespace_documents().items()
    }
    files["priorityclass.yaml"] = render_tag("cluster-base", _stubs()).files[
        "priorityclass.yaml"
    ]
    for cls in sorted(classes):
        doc = {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "StorageClass",
            "metadata": {"name": cls},
            "provisioner": STAND_IN_PROVISIONER,
            "volumeBindingMode": "WaitForFirstConsumer",
            "reclaimPolicy": "Delete",
        }
        files[f"storageclass-{cls}.yaml"] = yaml.safe_dump(doc, sort_keys=False)
    return files


def break_glass_source(pubkey: str) -> tuple[dict, dict]:
    """The flux role's break-glass OCIRepository and Secret, at the role's own address."""
    sync, secrets = _render_flux(
        {"flux_break_glass": "true", "flux_break_glass_public_key": pubkey}
    )
    source = next(
        d for d in sync if (d["kind"], d["metadata"]["name"]) == ("OCIRepository", ROOT)
    )
    source["spec"]["interval"] = "15s"
    name = yaml_fast.safe_load(FLUX_DEFAULTS.read_text())["flux_break_glass_secret"]
    secret = next(d for d in secrets if d["metadata"]["name"] == name)
    return source, secret


def push_target(source: dict) -> dict[str, str]:
    """The repository path and tag `source` follows, which the runner pushes to."""
    return {
        "repository": source["spec"]["url"].split("/", 3)[3],
        "tag": source["spec"]["ref"]["tag"],
    }


def build(out: Path, pubkey: str) -> None:
    """Write every file the fresh-cluster case reads under `out`."""
    registry = registry_files()
    source, secret = break_glass_source(pubkey)
    dump = yaml.safe_dump
    images = [
        {"ref": ref, "name": containerd_name(ref), "archive": f"image-{i}.tar"}
        for i, ref in enumerate(kit_images())
    ]
    (out / "kind.yaml").write_text(dump(kind_config(), sort_keys=False))
    (out / "images.json").write_text(json.dumps(images, indent=1) + "\n")
    (out / "push.json").write_text(json.dumps(push_target(source)) + "\n")
    _write_all(out / "prereq", prereq_files(claim_classes(registry)))
    _write_all(out / "registry", registry)
    _write_all(out / "bundle", bundle((FRESH,), drill_image(), keep_pvcs=False))
    _write_all(
        out / "hand",
        {
            "secret.yaml": dump(secret, sort_keys=False),
            "ocirepository.yaml": dump(source, sort_keys=False),
            "root.yaml": dump(production_root(), sort_keys=False),
        },
    )


# --- Judging the cluster --------------------------------------------------------------


def fresh_cluster_problems(items: list[dict], url: str, tag: str) -> list[str]:
    """Judge the key-signed bundle on the fresh cluster.

    The source must follow `url`, the in-cluster registry's address, and be verified. The
    bundle at `tag` must be served and applied, which its ConfigMap proves.
    """
    source = _find(items, "OCIRepository", ROOT, FLUX_NAMESPACE)
    revision = _artifact_revision(items)
    problems = []
    if (source or {}).get("spec", {}).get("url") != url:
        problems.append(f"OCIRepository {ROOT} does not follow {url}")
    if _condition(source, "Ready").get("status") != "True":
        message = _condition(source, "Ready").get("message", "no Ready condition")
        problems.append(f"OCIRepository {ROOT} is not Ready: {message[:200]}")
    if _condition(source, "SourceVerified").get("status") != "True":
        problems.append(f"OCIRepository {ROOT} reports no SourceVerified=True")
    if not revision.startswith(f"{tag}@"):
        problems.append(f"the served revision {revision!r} is not the {tag} bundle's")
    ks = _find(items, "Kustomization", FRESH, FLUX_NAMESPACE)
    if (ks or {}).get("status", {}).get("lastAppliedRevision") != revision:
        problems.append(f"Kustomization {FRESH} has not applied {revision}")
    if _find(items, "ConfigMap", FRESH_MARKER, FRESH) is None:
        problems.append(
            f"ConfigMap {FRESH_MARKER} is missing, so the bundle was not applied"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="write the files the fresh-cluster case applies")
    b.add_argument(
        "--bg-pubkey", type=Path, required=True, help="the throwaway public key"
    )
    b.add_argument("--out", type=Path, required=True, help="an absent directory")
    a = sub.add_parser(
        "archive", help="name a crane OCI layout for containerd and tar it"
    )
    a.add_argument("--layout", type=Path, required=True, help="the layout directory")
    a.add_argument("--ref", required=True, help="the digest-pinned reference it holds")
    a.add_argument("--out", type=Path, required=True, help="the tar to write")
    sub.add_parser("check", help="judge `kubectl get ... -o json` read from stdin")
    args = parser.parse_args(argv)
    if args.command == "build":
        args.out.mkdir(parents=True)
        build(args.out, args.bg_pubkey.read_text())
        print(f"built {args.out}")
        return OK
    if args.command == "archive":
        try:
            archive(args.layout, args.ref, args.out)
        except ValueError as err:
            print(f"archive: {err}", file=sys.stderr)
            return FAILED
        print(f"archived {args.ref} as {containerd_name(args.ref)}")
        return OK
    source, _ = break_glass_source("")
    target = push_target(source)
    items = json.load(sys.stdin)["items"]
    problems = fresh_cluster_problems(items, source["spec"]["url"], target["tag"])
    print(f"case={CASE} verdict={'fail' if problems else 'pass'}")
    print("\n".join(problems))
    return FAILED if problems else OK


if __name__ == "__main__":
    sys.exit(main())
