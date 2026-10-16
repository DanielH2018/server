"""`promote.py check-bundle`'s census: the bundle's `flux/` holds exactly what its inputs allow.

The render job runs it on `out/` before gitleaks and before any push, so a refusal publishes
nothing and `prod` stays where it was. `report` runs the source, host and no-stub gates of
`bundle_gates.py` beside it. Contract C3 *Publish census* defines the census:

- `flux/` holds `kustomization.yaml` plus one `<name>.yaml` for each published tag and each
  `SHARED_KINDS` member, and nothing else;
- `flux/kustomization.yaml` lists exactly the other files in `flux/`;
- each `<name>.yaml` holds exactly the Kustomization `<name>`, with path `./cluster/<name>`,
  plus `<name>-deferred` second, with path `./cluster/<name>-deferred`, exactly when `<name>`
  is a published tag that defers files (the per-file rule, D4.3);
- each published tag and each shared kind has its directory `cluster/<name>/`, and each tag
  that defers files `cluster/<name>-deferred/`, each with a `kustomization.yaml`;
- `digests.json` names each published tag and shared kind, plus `_unclassified`.

The allowlist comes from the renderer's inputs, never from its output, so a renderer bug
cannot widen it: the tags the inventory marks `owner: flux` (`published_tags`), the literal
`SHARED_KINDS`, and the published tags whose role passes `manifests_deferred_files` to its
`k8s/manifests` include (`deferring_tags`), read as the renderer reads the include. A tag marked `owner: released` is left out of both the bundle and the
allowlist (C4).

The plan is docs/target-architecture/deploy/d2-first-leaf.md, *Publication and verification*.

Run: uv run python scripts/deploy_tools/promote.py check-bundle out
     uv run python scripts/deploy_tools/promote.py published-tags
"""

import argparse
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
from lib.repo_paths import K8S_ROLES
import bundle_gates
from renderer.renderer_lib.bundle import (
    CLUSTER_DIR,
    DEFERRED_SUFFIX,
    DIGESTS,
    FLUX_API_VERSION,
    FLUX_DIR,
    KUSTOMIZATION,
    SHARED_KINDS,
    UNCLASSIFIED,
)
from renderer.renderer_lib.task_inputs import include_vars


def published_tags(host_vars: Path = K8S_HOST_VARS) -> list[str]:
    """The sorted deploy tags the bundle publishes: those the inventory marks `owner: flux`."""
    return sorted(
        tag for tag, owner in tag_owners(host_vars).items() if owner == "flux"
    )


def deferring_tags(
    published: Iterable[str], roles: Path = K8S_ROLES
) -> tuple[frozenset[str], list[str]]:
    """The published tags that defer files, and one line per tag it could not judge.

    A tag defers files when its role passes a non-empty `manifests_deferred_files` to the
    `k8s/manifests` include that renders, which alone makes the renderer write
    `cluster/<tag>-deferred/` and the Kustomization `<tag>-deferred`. A value that is not a
    literal list, such as a Jinja string, is a problem rather than a guess.
    """
    found, problems = set(), []
    for tag in sorted(set(published)):
        try:
            value = include_vars(roles / tag).get("manifests_deferred_files")
        except (OSError, ValueError) as err:
            problems.append(f"could not tell whether {tag} defers files: {err}")
            continue
        if value is not None and not isinstance(value, list):
            problems.append(
                f"could not tell whether {tag} defers files: its "
                f"manifests_deferred_files is {value!r}, not a literal list"
            )
        elif value:
            found.add(tag)
    return frozenset(found), problems


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


def _kustomization_problems(flux: Path, name: str, defers: bool) -> list[str]:
    """What is wrong with `flux/<name>.yaml` under the per-file rule.

    It must hold exactly the Kustomization `name`, plus `<name>-deferred` second when
    `defers`, each applying its own directory under `cluster/`.
    """
    docs = _documents(flux / f"{name}.yaml")
    if isinstance(docs, str):
        return [f"{FLUX_DIR}/{name}.yaml {docs}"]
    names = [name] + ([name + DEFERRED_SUFFIX] if defers else [])
    found = [
        (d.get("apiVersion"), d.get("kind"), (d.get("metadata") or {}).get("name"))
        if isinstance(d, dict)
        else (None, None, None)
        for d in docs
    ]
    if found != [(FLUX_API_VERSION, "Kustomization", n) for n in names]:
        return [
            f"{FLUX_DIR}/{name}.yaml holds {found}, not exactly the Kustomization "
            + " then the Kustomization ".join(names)
        ]
    problems = []
    for doc, own in zip(docs, names, strict=True):
        spec = doc.get("spec") if isinstance(doc, dict) else None
        path = spec.get("path") if isinstance(spec, dict) else None
        if path != f"./{CLUSTER_DIR}/{own}":
            problems.append(
                f"{FLUX_DIR}/{name}.yaml's {own} applies {path!r}, not ./{CLUSTER_DIR}/{own}"
            )
    return problems


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
    out: Path,
    published: Iterable[str],
    kinds: Iterable[str] = SHARED_KINDS,
    deferring: Iterable[str] = (),
) -> list[str]:
    """Every way the bundle at `out` differs from the allowlist, one line each.

    Args:
        out: The bundle root, holding `flux/`, `cluster/` and `digests.json`.
        published: The published tags, from the inventory.
        kinds: The shared kinds.
        deferring: The published tags that defer files, from their roles (`deferring_tags`).
            A shared kind never defers.
    """
    published, kinds = set(published), set(kinds)
    deferring = set(deferring) & published
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
            found += _kustomization_problems(flux, name, name in deferring)
    found += [
        f"{CLUSTER_DIR}/{name}/ is missing: {name} is published and renders no directory"
        for name in sorted(names)
        if not (out / CLUSTER_DIR / name / KUSTOMIZATION).is_file()
    ] + [
        f"{CLUSTER_DIR}/{tag}{DEFERRED_SUFFIX}/ is missing: {tag} defers files and renders "
        "no deferred directory"
        for tag in sorted(deferring)
        if not (out / CLUSTER_DIR / f"{tag}{DEFERRED_SUFFIX}" / KUSTOMIZATION).is_file()
    ]
    return found + _digests_problems(out, names)


def report(out: Path, published: list[str], roles: Path = K8S_ROLES) -> int:
    """Print the census's and the gates' problems, or one line saying both passed.

    Returns the exit code.
    """
    deferring, problems = deferring_tags(published, roles)
    problems += census(out, published, deferring=deferring)
    problems += bundle_gates.gates(out, published)
    for line in problems:
        print(f"check-bundle: {line}", file=sys.stderr)
    if problems:
        print(
            f"check-bundle: {len(problems)} problems; nothing may be published",
            file=sys.stderr,
        )
        return FAILED
    print(
        f"check-bundle: the census and the source, host and no-stub gates pass: "
        f"{len(published)} published tags "
        f"({', '.join(published) or 'none'}), {len(SHARED_KINDS)} shared kinds"
    )
    return OK


# The subcommands `promote.py` hands to `run`. Both read the checkout alone, in the render
# job, which holds no token.
COMMANDS = ("check-bundle", "published-tags")


def add_parsers(commands) -> None:
    """Add the `check-bundle` and `published-tags` subcommands to `promote.py`'s subparsers."""
    checker = commands.add_parser(
        "check-bundle",
        help="run the publish census and gates over a rendered bundle",
    )
    checker.add_argument("out", type=Path, help="the bundle root the render job built")
    lister = commands.add_parser(
        "published-tags",
        help="print the deploy tags the bundle publishes, one per line",
    )
    for sub in (checker, lister):
        sub.add_argument(
            "--host-vars",
            type=Path,
            default=K8S_HOST_VARS,
            help="the inventory file whose owner: flux entries the bundle publishes",
        )


def run(args: argparse.Namespace) -> int:
    """Run `check-bundle` or `published-tags`, and return the exit code."""
    published = published_tags(args.host_vars)
    if args.command == "published-tags":
        for tag in published:
            print(tag)
        return OK
    return report(args.out, published)
