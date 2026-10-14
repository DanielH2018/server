"""`promote.py check-bundle`'s census: the bundle's `flux/` holds exactly what its inputs allow.

The render job runs it on `out/` before gitleaks and before any push, so a refusal publishes
nothing and `prod` stays where it was. Contract C3 *Publish census* defines it:

- `flux/` holds `kustomization.yaml` plus one `<name>.yaml` for each published tag and each
  `SHARED_KINDS` member, and nothing else;
- `flux/kustomization.yaml` lists exactly the other files in `flux/`;
- each `<name>.yaml` holds exactly the Kustomization `<name>`, with path `./cluster/<name>`;
- each published tag has its directory `cluster/<tag>/`;
- `digests.json` names each published tag and shared kind, plus `_unclassified`.

The allowlist comes from the renderer's inputs, never from its output, so a renderer bug
cannot widen it: the tags the inventory marks `owner: flux` (`published_tags`) and the literal
`SHARED_KINDS`. A tag marked `owner: released` is left out of both the bundle and the
allowlist (C4).

D2.6c adds the source, host and no-stub gates beside the census. The plan is
docs/target-architecture/deploy/d2-first-leaf.md, *Publication and verification*.

Run: uv run python scripts/deploy_tools/promote.py check-bundle out
     uv run python scripts/deploy_tools/promote.py published-tags
"""

import json
import sys
from collections.abc import Iterable
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
import yaml

from lib import yaml_fast
from lib.exit_codes import FAILED, OK
from lib.ansible_inventory import K8S_HOST_VARS
from lib.k8s_roles import tag_owners
from renderer.renderer_lib.bundle import (
    CLUSTER_DIR,
    DIGESTS,
    FLUX_API_VERSION,
    FLUX_DIR,
    KUSTOMIZATION,
    SHARED_KINDS,
    UNCLASSIFIED,
)


def published_tags(host_vars: Path = K8S_HOST_VARS) -> list[str]:
    """The sorted deploy tags the bundle publishes: those the inventory marks `owner: flux`."""
    return sorted(
        tag for tag, owner in tag_owners(host_vars).items() if owner == "flux"
    )


def _documents(path: Path) -> list[object] | str:
    """The parsed documents of `path`, or the parse error as text."""
    try:
        return [d for d in yaml_fast.safe_load_all(path.read_text()) if d is not None]
    except yaml.YAMLError as exc:
        return f"does not parse: {exc}"


def _index_problems(flux: Path, held: set[str]) -> list[str]:
    """What is wrong with `flux/kustomization.yaml`'s `resources` against the files held."""
    docs = _documents(flux / KUSTOMIZATION)
    if isinstance(docs, str):
        return [f"{FLUX_DIR}/{KUSTOMIZATION} {docs}"]
    resources = (
        docs[0].get("resources")
        if len(docs) == 1 and isinstance(docs[0], dict)
        else None
    )
    if not isinstance(resources, list):
        return [f"{FLUX_DIR}/{KUSTOMIZATION} holds no resources list"]
    listed = {str(r) for r in resources}
    others = held - {KUSTOMIZATION}
    return [
        f"{FLUX_DIR}/{KUSTOMIZATION} does not list {name}, which flux/ holds"
        for name in sorted(others - listed)
    ] + [
        f"{FLUX_DIR}/{KUSTOMIZATION} lists {name}, which flux/ does not hold"
        for name in sorted(listed - others)
    ]


def _kustomization_problems(flux: Path, name: str) -> list[str]:
    """What is wrong with `flux/<name>.yaml`: it must hold exactly the Kustomization `name`."""
    docs = _documents(flux / f"{name}.yaml")
    if isinstance(docs, str):
        return [f"{FLUX_DIR}/{name}.yaml {docs}"]
    found = [
        (d.get("apiVersion"), d.get("kind"), (d.get("metadata") or {}).get("name"))
        if isinstance(d, dict)
        else (None, None, None)
        for d in docs
    ]
    if found != [(FLUX_API_VERSION, "Kustomization", name)] or not isinstance(
        docs[0], dict
    ):
        return [
            f"{FLUX_DIR}/{name}.yaml holds {found}, not exactly the Kustomization {name}"
        ]
    path = (docs[0].get("spec") or {}).get("path")
    if path != f"./{CLUSTER_DIR}/{name}":
        return [f"{FLUX_DIR}/{name}.yaml applies {path!r}, not ./{CLUSTER_DIR}/{name}"]
    return []


def _digests_problems(out: Path, names: set[str]) -> list[str]:
    """What is wrong with the names `digests.json` maps."""
    path = out / DIGESTS
    if not path.is_file():
        return [f"{DIGESTS} is missing"]
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [f"{DIGESTS} is not JSON: {exc}"]
    expected = names | {UNCLASSIFIED}
    if not isinstance(value, dict) or set(value) != expected:
        held = sorted(value) if isinstance(value, dict) else type(value).__name__
        return [f"{DIGESTS} names {held}, not {sorted(expected)}"]
    return []


def census(
    out: Path, published: Iterable[str], kinds: Iterable[str] = SHARED_KINDS
) -> list[str]:
    """Every way the bundle at `out` differs from the allowlist, one line each.

    Args:
        out: The bundle root, holding `flux/`, `cluster/` and `digests.json`.
        published: The published tags, from the inventory.
        kinds: The shared kinds.
    """
    published, kinds = set(published), set(kinds)
    names = published | kinds
    flux = out / FLUX_DIR
    if not flux.is_dir():
        return [f"{FLUX_DIR}/ is missing"]
    held = {p.relative_to(flux).as_posix() for p in flux.rglob("*") if p.is_file()}
    expected = {KUSTOMIZATION} | {f"{name}.yaml" for name in names}
    found = [
        f"{FLUX_DIR}/{file} is missing: {file.removesuffix('.yaml')} is published and "
        "renders no file"
        for file in sorted(expected - held)
    ] + [
        f"{FLUX_DIR}/{file} is not a published tag's or a shared kind's file"
        for file in sorted(held - expected)
    ]
    if KUSTOMIZATION in held:
        found += _index_problems(flux, held)
    for name in sorted(names):
        if f"{name}.yaml" in held:
            found += _kustomization_problems(flux, name)
    found += [
        f"{CLUSTER_DIR}/{tag}/ is missing: {tag} is published and renders no directory"
        for tag in sorted(published)
        if not (out / CLUSTER_DIR / tag / KUSTOMIZATION).is_file()
    ]
    return found + _digests_problems(out, names)


def report(out: Path, published: list[str]) -> int:
    """Print the census's problems, or one line saying it passed, and return the exit code."""
    problems = census(out, published)
    for line in problems:
        print(f"check-bundle: {line}", file=sys.stderr)
    if problems:
        print(
            f"check-bundle: {len(problems)} problems; nothing may be published",
            file=sys.stderr,
        )
        return FAILED
    print(
        f"check-bundle: the census passes: {len(published)} published tags "
        f"({', '.join(published) or 'none'}), {len(SHARED_KINDS)} shared kinds"
    )
    return OK
