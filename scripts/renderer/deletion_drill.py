#!/usr/bin/env python3
"""Fixture bundles and verdicts for the Flux deletion drill (D6.11, `flux-deletion-drill.yml`).

The drill proves, on a throwaway kind cluster, what kustomize-controller does when a tag
leaves the bundle, and that break-glass promotion verifies a key-signed bundle. This module
builds what the workflow pushes and applies, and judges what the cluster reports back. It runs
no `kubectl`, `flux` or `cosign`: the workflow pipes `kubectl get -o json` into `check`, so
every verdict is testable on fixture JSON.

The bundles come from the renderer's own writers, never from a hand-written Kustomization:
`prune_label.label` stamps the prune marker, and `bundle.flux_files` and
`bundle.kustomization_text` write each tag's Kustomization and the root's `resources`. The
drill therefore proves that the form the renderer writes is one that Flux honours. It does not
run the `homelab-render cluster` CLI, which renders the repo's own tree.

- `build` writes `rev1/`, `rev2/`, `bg/` and `bg-red/` (the artifacts the registry serves),
  `hand/` (the documents applied by hand) and `expect.json`.
- `check <case>` prints one `case=<name> verdict=<pass|fail>` line, then one problem per line,
  and exits 1 when there is any.

Run (the workflow's form):
    uv run python scripts/renderer/deletion_drill.py build --registry kind-registry:5000 \
        --bg-pubkey bg.pub --bg-red-pubkey red.pub --out DIR
"""

import json
import re
import sys
import sys as _sys
from collections.abc import Mapping
from pathlib import Path
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

import yaml
from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK
from lib.repo_paths import K8S_ROLES
from renderer.homelab_render import secret_keys
from renderer.renderer_lib.bundle import flux_files, kustomization_text
from renderer.renderer_lib.cluster import render_tag
from renderer.renderer_lib.placeholders import placeholder_vars
from renderer.renderer_lib.prune_label import PRUNE_DISABLED, PRUNE_LABEL, label

FIXTURES = _Path(__file__).resolve().parent / "tests" / "fixtures" / "deletion_drill"
FLUX_DEFAULTS = K8S_ROLES / "flux" / "defaults" / "main.yml"

AS_RENDERED = "prune-drill"
RED = "prune-drill-red"
KEEP = "prune-drill-keep"
TAGS = (AS_RENDERED, RED, KEEP)
# The tags revision 2 drops from the bundle, so the root prunes their Kustomizations.
DROPPED = (AS_RENDERED, RED)
KEEP_MARKED = "keep-marked"
KEEP_UNMARKED = "keep-unmarked"
IMAGE_TOKEN = "@DRILL_IMAGE@"
FLUX_NAMESPACE = "flux-system"
ROOT = "homelab"
TIMEOUT = "3m"

DRILL_REPO = "drill"
BG_REPO = "bg"
BG_RED_REPO = "bg-red"
BG_MARKER = "bg-marker"
BG_RED_MARKER = "bg-red-marker"
BG_SECRET = "flux-break-glass-key"
# What a failed verification says, as source-controller words it. A registry or transport
# failure carries none of these, so it cannot read as a refusal.
VERIFICATION_WORDS = re.compile(r"verif|signature|cosign", re.IGNORECASE)


# --- Building the bundles -------------------------------------------------------------


def drill_image() -> str:
    """The image every fixture Deployment runs: the flux role's `flux_job_image` pin."""
    return yaml_fast.safe_load(FLUX_DEFAULTS.read_text())["flux_job_image"]


def strip_marker(text: str) -> str:
    """`text` without the prune marker line `prune_label.label` inserted."""
    entry = f"{PRUNE_LABEL}: {PRUNE_DISABLED}"
    return "".join(line for line in text.splitlines(keepends=True) if entry not in line)


def tag_directory(
    tag: str, image: str, *, unmarked: frozenset[str] = frozenset(), drop_pvcs=False
) -> dict[str, str]:
    """Each file of `tag`'s `cluster/<tag>/` directory, stamped as the renderer stamps it.

    Args:
        tag: A fixture tag.
        image: The text that replaces `IMAGE_TOKEN`.
        unmarked: File names whose prune marker is stripped again, for a red half.
        drop_pvcs: Leave out every claim, as revision 2 of `prune-drill-keep` does.
    """
    files = {}
    for path in sorted((FIXTURES / tag).glob("*.yaml")):
        if drop_pvcs and path.name.startswith("pvc"):
            continue
        text = label(path.read_text().replace(IMAGE_TOKEN, image), f"{tag}/{path.name}")
        files[path.name] = strip_marker(text) if path.name in unmarked else text
    files["kustomization.yaml"] = kustomization_text(files)
    return files


def bundle(
    tags: tuple[str, ...],
    image: str,
    *,
    keep_pvcs: bool,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The bundle publishing `tags`, by path: `cluster/<tag>/*`, `flux/<tag>.yaml` and the index.

    Revision 1 strips the markers the red halves need stripped, both in the claims and in
    `prune-drill-red`'s Kustomization. `extra` adds files to `cluster/prune-drill-keep/`.
    """
    files: dict[str, str] = {}
    for tag in tags:
        unmarked = {RED: {"pvc.yaml"}, KEEP: {"pvc-keep-unmarked.yaml"}}.get(tag, set())
        held = tag_directory(
            tag,
            image,
            unmarked=frozenset(unmarked) if keep_pvcs else frozenset(),
            drop_pvcs=tag == KEEP and not keep_pvcs,
        )
        if tag == KEEP:
            for name, text in (extra or {}).items():
                held[name] = text
            held.pop("kustomization.yaml")
            held["kustomization.yaml"] = kustomization_text(held)
        files |= {f"cluster/{tag}/{name}": text for name, text in held.items()}
    files |= flux_files({tag: False for tag in tags}, TIMEOUT)
    if RED in tags and keep_pvcs:
        files[f"flux/{RED}.yaml"] = strip_delete_policy(files[f"flux/{RED}.yaml"])
    return files


def strip_delete_policy(text: str) -> str:
    """`text`, a Kustomization stream, with `spec.deletionPolicy` removed from every document."""
    docs = list(yaml_fast.safe_load_all(text))
    for doc in docs:
        doc["spec"].pop("deletionPolicy", None)
    return yaml.safe_dump_all(docs, sort_keys=False)


def rendered_policy(files: Mapping[str, str], tag: str) -> str:
    """The `deletionPolicy` the bundle's Kustomization for `tag` declares, or `default`."""
    docs = yaml_fast.safe_load_all(files[f"flux/{tag}.yaml"])
    spec = next(d for d in docs if d["metadata"]["name"] == tag)["spec"]
    return spec.get("deletionPolicy", "default")


def marker_configmap(name: str) -> dict[str, str]:
    """A ConfigMap file for `cluster/prune-drill-keep/`, whose presence proves an apply."""
    doc = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": KEEP},
        "data": {"applied": "true"},
    }
    return {f"{name}.yaml": yaml.safe_dump(doc, sort_keys=False)}


def drill_source(registry: str) -> dict:
    """The drill's own OCIRepository `homelab`: the local registry, plain HTTP, no `verify`."""
    return {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "OCIRepository",
        "metadata": {"name": ROOT, "namespace": FLUX_NAMESPACE},
        "spec": {
            "interval": "15s",
            "url": f"oci://{registry}/{DRILL_REPO}",
            "insecure": True,
            "ref": {"tag": DRILL_REPO},
        },
    }


def _render_flux(overrides: Mapping[str, str]) -> tuple[list[dict], list[dict]]:
    """The flux role's sync documents and Secret documents, rendered with `overrides`."""
    rendered = render_tag("flux", {**placeholder_vars(secret_keys()), **overrides})
    sync = yaml_fast.safe_load_all(rendered.deferred["sync.yaml"])
    secret = yaml_fast.safe_load_all(rendered.files["secret.yaml"])
    return [d for d in sync if d], [d for d in secret if d]


def break_glass_documents(registry: str, repo: str, pubkey: str) -> tuple[dict, dict]:
    """The flux role's break-glass OCIRepository `homelab` and its public-key Secret.

    Both come from the role's own templates with `flux_break_glass` on, so the drill proves
    the form the runbook commits. `repo` is the registry path the OCIRepository follows, and
    it doubles as the tag, so the revision of each artifact names the bundle it came from.
    """
    sync, secrets = _render_flux(
        {
            "flux_break_glass": "true",
            "flux_break_glass_url": f"oci://{registry}/{repo}",
            "flux_break_glass_tag": repo,
            "flux_break_glass_public_key": pubkey,
        }
    )
    source = next(
        d for d in sync if (d["kind"], d["metadata"]["name"]) == ("OCIRepository", ROOT)
    )
    source["spec"]["interval"] = "15s"
    secret = next(d for d in secrets if d["metadata"]["name"] == BG_SECRET)
    return source, secret


def production_root() -> dict:
    """The root Kustomization `homelab`, as the flux role renders it for production."""
    sync, _ = _render_flux({})
    return next(
        d for d in sync if (d["kind"], d["metadata"]["name"]) == ("Kustomization", ROOT)
    )


def _write_all(root: Path, files: Mapping[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def build(out: Path, registry: str, pubkey: str, red_pubkey: str) -> None:
    """Write `rev1/`, `rev2/`, `bg/`, `bg-red/`, `hand/` and `expect.json` under `out`."""
    image = drill_image()
    rev1 = bundle(TAGS, image, keep_pvcs=True)
    rev2 = bundle((KEEP,), image, keep_pvcs=False)
    green = bundle((KEEP,), image, keep_pvcs=False, extra=marker_configmap(BG_MARKER))
    red = bundle((KEEP,), image, keep_pvcs=False, extra=marker_configmap(BG_RED_MARKER))
    for name, files in (("rev1", rev1), ("rev2", rev2), ("bg", green), ("bg-red", red)):
        _write_all(out / name, files)
    green_source, secret = break_glass_documents(registry, BG_REPO, pubkey)
    red_source, _ = break_glass_documents(registry, BG_RED_REPO, red_pubkey)
    dump = yaml.safe_dump
    _write_all(
        out / "hand",
        {
            "ocirepository.yaml": dump(drill_source(registry), sort_keys=False),
            "root.yaml": dump(production_root(), sort_keys=False),
            "bg-secret.yaml": dump(secret, sort_keys=False),
            "bg-ocirepository.yaml": dump(green_source, sort_keys=False),
            "bg-red-ocirepository.yaml": dump(red_source, sort_keys=False),
        },
    )
    expect = {"policy": rendered_policy(rev1, AS_RENDERED)}
    (out / "expect.json").write_text(json.dumps(expect) + "\n")


# --- Judging the cluster --------------------------------------------------------------


def _find(
    items: list[dict], kind: str, name: str, namespace: str | None
) -> dict | None:
    for item in items:
        meta = item.get("metadata", {})
        if (item.get("kind"), meta.get("name"), meta.get("namespace")) == (
            kind,
            name,
            namespace,
        ):
            return item
    return None


def _condition(obj: dict | None, kind: str) -> dict:
    for cond in (obj or {}).get("status", {}).get("conditions", []):
        if cond.get("type") == kind:
            return cond
    return {}


def _survives(obj: dict | None) -> bool:
    """Whether `obj` exists and carries no deletion timestamp.

    A PVC's phase is not the check: while a pod still mounts a deleted claim,
    `pvc-protection` holds it with a deletion timestamp and its phase still reads Bound.
    """
    return obj is not None and not obj["metadata"].get("deletionTimestamp")


def _kept(items: list[dict], kind: str, name: str, namespace: str | None) -> list[str]:
    obj = _find(items, kind, name, namespace)
    if obj is None:
        return [f"{kind} {namespace}/{name} is gone"]
    if not _survives(obj):
        return [f"{kind} {namespace}/{name} is terminating"]
    return []


def _lost(items: list[dict], kind: str, name: str, namespace: str) -> list[str]:
    if _survives(_find(items, kind, name, namespace)):
        return [
            f"{kind} {namespace}/{name} survived, so this case cannot see a lost one"
        ]
    return []


def revision_1_problems(items: list[dict]) -> list[str]:
    """Judge the wait after revision 1: all three tags Ready, every claim Bound."""
    problems = []
    for tag in TAGS:
        ks = _find(items, "Kustomization", tag, FLUX_NAMESPACE)
        if _condition(ks, "Ready").get("status") != "True":
            message = _condition(ks, "Ready").get("message", "no Ready condition")
            problems.append(f"Kustomization {tag} is not Ready: {message[:200]}")
    for ns, claim in (
        (AS_RENDERED, "data"),
        (RED, "data"),
        (KEEP, KEEP_MARKED),
        (KEEP, KEEP_UNMARKED),
    ):
        pvc = _find(items, "PersistentVolumeClaim", claim, ns)
        if (pvc or {}).get("status", {}).get("phase") != "Bound":
            problems.append(f"PersistentVolumeClaim {ns}/{claim} is not Bound")
    return problems


def as_rendered_problems(items: list[dict], policy: str) -> list[str]:
    """Judge `prune-drill` once revision 2 dropped it, under the policy it declared.

    The Namespace and the PVC stay under both policies. The Deployment stays only under
    `Orphan`.
    """
    problems = []
    if _find(items, "Kustomization", AS_RENDERED, FLUX_NAMESPACE):
        problems.append(f"Kustomization {AS_RENDERED} still exists")
    deployment = _find(items, "Deployment", "holder", AS_RENDERED)
    if policy == "Orphan" and deployment is None:
        problems.append("the Deployment is gone although the policy is Orphan")
    if policy != "Orphan" and deployment is not None:
        problems.append(f"the Deployment stays although the policy is {policy}")
    problems += _kept(items, "PersistentVolumeClaim", "data", AS_RENDERED)
    problems += _kept(items, "Namespace", AS_RENDERED, None)
    return problems


def red_half_problems(items: list[dict]) -> list[str]:
    """Judge `prune-drill-red`: its unmarked claim must be gone or terminating."""
    problems = []
    if _find(items, "Kustomization", RED, FLUX_NAMESPACE):
        problems.append(f"Kustomization {RED} still exists")
    return problems + _lost(items, "PersistentVolumeClaim", "data", RED)


def inventory_prune_problems(items: list[dict]) -> list[str]:
    """Judge `prune-drill-keep` at revision 2.

    `keep-marked` survives, and the unmarked claim, which a pod still mounts, is gone or
    terminating. The tag must have applied the revision the source serves.
    """
    ks = _find(items, "Kustomization", KEEP, FLUX_NAMESPACE)
    revision = _artifact_revision(items)
    problems = []
    if not revision.startswith(f"{DRILL_REPO}@"):
        problems.append(f"the served revision {revision!r} is not the drill bundle's")
    if _condition(ks, "Ready").get("status") != "True":
        problems.append(f"Kustomization {KEEP} is not Ready")
    if (ks or {}).get("status", {}).get("lastAppliedRevision") != revision:
        problems.append(f"Kustomization {KEEP} has not applied {revision}")
    problems += _kept(items, "PersistentVolumeClaim", KEEP_MARKED, KEEP)
    return problems + _lost(items, "PersistentVolumeClaim", KEEP_UNMARKED, KEEP)


def _artifact_revision(items: list[dict]) -> str:
    source = _find(items, "OCIRepository", ROOT, FLUX_NAMESPACE)
    return (source or {}).get("status", {}).get("artifact", {}).get("revision", "")


def break_glass_problems(items: list[dict]) -> list[str]:
    """Judge the key-signed bundle: verified, served, and applied by `prune-drill-keep`."""
    source = _find(items, "OCIRepository", ROOT, FLUX_NAMESPACE)
    revision = _artifact_revision(items)
    problems = []
    if _condition(source, "Ready").get("status") != "True":
        message = _condition(source, "Ready").get("message", "no Ready condition")
        problems.append(f"OCIRepository {ROOT} is not Ready: {message[:200]}")
    if _condition(source, "SourceVerified").get("status") != "True":
        problems.append(f"OCIRepository {ROOT} reports no SourceVerified=True")
    if not revision.startswith(f"{BG_REPO}@"):
        problems.append(
            f"the served revision {revision!r} is not the {BG_REPO} bundle's"
        )
    ks = _find(items, "Kustomization", KEEP, FLUX_NAMESPACE)
    if (ks or {}).get("status", {}).get("lastAppliedRevision") != revision:
        problems.append(f"Kustomization {KEEP} has not applied {revision}")
    if _find(items, "ConfigMap", BG_MARKER, KEEP) is None:
        problems.append(
            f"ConfigMap {BG_MARKER} is missing, so the bundle was not applied"
        )
    return problems


def break_glass_red_problems(items: list[dict]) -> list[str]:
    """Judge the bundle signed by a key the cluster does not hold: refused, and not applied.

    The refusal must name verification. The served revision must stay the green bundle's, and
    the red bundle's ConfigMap must not exist.
    """
    source = _find(items, "OCIRepository", ROOT, FLUX_NAMESPACE)
    ready = _condition(source, "Ready")
    problems = []
    meta = (source or {}).get("metadata", {})
    if ready.get("observedGeneration") != meta.get("generation"):
        problems.append("the OCIRepository has not yet judged the red source")
    elif ready.get("status") == "True":
        problems.append(
            f"OCIRepository {ROOT} is Ready on a bundle signed by another key"
        )
    elif not VERIFICATION_WORDS.search(ready.get("message", "")):
        problems.append(
            f"OCIRepository {ROOT} failed, but not on verification: "
            f"{ready.get('message', '')[:200]!r}"
        )
    if _artifact_revision(items).startswith(f"{BG_RED_REPO}@"):
        problems.append("source-controller served the red bundle")
    if _find(items, "ConfigMap", BG_RED_MARKER, KEEP) is not None:
        problems.append(
            f"ConfigMap {BG_RED_MARKER} exists, so the red bundle was applied"
        )
    if not _artifact_revision(items).startswith(f"{BG_REPO}@"):
        problems.append(
            "the green bundle is no longer served, so this verdict is unjudged"
        )
    return problems


CASES = {
    "revision-1": lambda items, args: revision_1_problems(items),
    "as-rendered": lambda items, args: as_rendered_problems(items, args.policy),
    "red-half": lambda items, args: red_half_problems(items),
    "inventory-prune": lambda items, args: inventory_prune_problems(items),
    "break-glass": lambda items, args: break_glass_problems(items),
    "break-glass-red": lambda items, args: break_glass_red_problems(items),
}


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="write the bundles and the hand-applied files")
    b.add_argument(
        "--registry", required=True, help="HOST:PORT source-controller pulls from"
    )
    b.add_argument(
        "--bg-pubkey", type=Path, required=True, help="the throwaway public key"
    )
    b.add_argument("--bg-red-pubkey", type=Path, required=True, help="the second one")
    b.add_argument("--out", type=Path, required=True, help="an absent directory")
    c = sub.add_parser("check", help="judge `kubectl get ... -o json` read from stdin")
    c.add_argument("case", choices=sorted(CASES))
    c.add_argument(
        "--policy", default="Orphan", help="as-rendered: the declared policy"
    )
    c.add_argument(
        "--revision", default="", help="inventory-prune: the served revision"
    )
    args = parser.parse_args(argv)
    if args.command == "build":
        args.out.mkdir(parents=True)
        build(
            args.out,
            args.registry,
            args.bg_pubkey.read_text(),
            args.bg_red_pubkey.read_text(),
        )
        print(f"built {args.out}")
        return OK
    problems = CASES[args.case](json.load(sys.stdin)["items"], args)
    print(f"case={args.case} verdict={'fail' if problems else 'pass'}")
    print("\n".join(problems))
    return FAILED if problems else OK


if __name__ == "__main__":
    sys.exit(main())
