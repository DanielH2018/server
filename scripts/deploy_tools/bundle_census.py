"""`promote.py check-bundle`'s census: the bundle's `flux/` holds exactly what its inputs allow.

The render job runs it on `out/` before gitleaks and before any push, so a refusal publishes
nothing and `prod` stays where it was. `report` runs the source, host and no-stub gates of
`bundle_gates.py` beside it. Contract C3 *Publish census* defines the census:

- `flux/` holds `kustomization.yaml` plus one `<name>.yaml` for each published tag and each
  `SHARED_KINDS` member, and nothing else;
- `flux/kustomization.yaml` lists exactly the other files in `flux/`;
- each `<name>.yaml` holds exactly the Kustomization `<name>`, with path `./cluster/<name>`,
  then each per-tag kind its role's inputs call for, in `TAG_KIND_SUFFIXES` order, each with
  path `./cluster/<name><suffix>` (the per-file rule, D4.3 and D5.1a1): `-deferred` when the
  role passes `manifests_deferred_files`, `-snapshot` when it declares
  `k8s_autodeploy_snapshot_pvcs`, and `-verify` for none yet;
- each published tag and each shared kind has its directory `cluster/<name>/`, and each
  per-tag kind its directory `cluster/<name><suffix>/`, each with a `kustomization.yaml`;
- no published tag takes a reserved name (`reserved_tags`), so a tag named `soak` fails
  here even before `soak` joins `SHARED_KINDS`;
- `digests.json` names each published tag and shared kind, plus `_unclassified`.

The allowlist comes from the renderer's inputs, never from its output, so a renderer bug
cannot widen it: the tags the inventory marks `owner: flux` (`published_tags`), the literal
`SHARED_KINDS`, and the published tags whose role passes `manifests_deferred_files` to its
`k8s/manifests` include (`deferring_tags`), read as the renderer reads the include, and those
whose role declares snapshot claims (`snapshot_tags`). No role input names a verify Job yet,
so `report` grants `-verify` to no tag, and the first PR that renders one adds its reader. A
tag marked `owner: released` is left out of both the bundle and the allowlist (C4).

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
    DIGESTS,
    FLUX_API_VERSION,
    FLUX_DIR,
    KUSTOMIZATION,
    SHARED_KINDS,
    UNCLASSIFIED,
    reserved_tags,
)
from renderer.renderer_lib.ordering import (
    DEFERRED_SUFFIX,
    SNAPSHOT_SUFFIX,
    TAG_KIND_SUFFIXES,
    VERIFY_SUFFIX,
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


def snapshot_tags(
    published: Iterable[str], roles: Path = K8S_ROLES
) -> tuple[frozenset[str], list[str]]:
    """The published tags that declare snapshot claims, and one line per tag it could not judge.

    A tag declares them when its role's `defaults/main.yml` sets a non-empty
    `k8s_autodeploy_snapshot_pvcs`, which alone calls for `<tag>-snapshot` (D5.2a). A value
    that is not a literal list is a problem rather than a guess.
    """
    found, problems = set(), []
    for tag in sorted(set(published)):
        path = roles / tag / "defaults" / "main.yml"
        try:
            defaults = yaml_fast.safe_load(path.read_text()) if path.is_file() else {}
        except (OSError, yaml.YAMLError) as err:
            problems.append(f"could not tell whether {tag} snapshots claims: {err}")
            continue
        value = (defaults or {}).get("k8s_autodeploy_snapshot_pvcs")
        if value is not None and not isinstance(value, list):
            problems.append(
                f"could not tell whether {tag} snapshots claims: its "
                f"k8s_autodeploy_snapshot_pvcs is {value!r}, not a literal list"
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


def _kustomization_problems(flux: Path, name: str, suffixes: list[str]) -> list[str]:
    """What is wrong with `flux/<name>.yaml` under the per-file rule.

    It must hold exactly the Kustomization `name`, then `<name><suffix>` for each of
    `suffixes` in order, each applying its own directory under `cluster/`.
    """
    docs = _documents(flux / f"{name}.yaml")
    if isinstance(docs, str):
        return [f"{FLUX_DIR}/{name}.yaml {docs}"]
    names = [name] + [name + suffix for suffix in suffixes]
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
    snapshotting: Iterable[str] = (),
    verifying: Iterable[str] = (),
) -> list[str]:
    """Every way the bundle at `out` differs from the allowlist, one line each.

    Args:
        out: The bundle root, holding `flux/`, `cluster/` and `digests.json`.
        published: The published tags, from the inventory.
        kinds: The shared kinds.
        deferring: The published tags that defer files, from their roles (`deferring_tags`).
        snapshotting: The published tags that declare snapshot claims (`snapshot_tags`).
        verifying: The published tags with a verify or post-apply mutation Job.

    A shared kind has no per-tag kind.
    """
    published, kinds = set(published), set(kinds)
    granted = {
        DEFERRED_SUFFIX: set(deferring) & published,
        SNAPSHOT_SUFFIX: set(snapshotting) & published,
        VERIFY_SUFFIX: set(verifying) & published,
    }
    suffixes = {
        tag: [s for s in TAG_KIND_SUFFIXES if tag in granted[s]] for tag in published
    }
    names = published | kinds
    reserved = [
        f"{tag} is published under a name the bundle reserves: the root's, a shared "
        "kind's, a RESERVED_NAMES member's, or one ending in a per-tag kind's suffix"
        for tag in reserved_tags(published, kinds)
    ]
    flux = out / FLUX_DIR
    if not flux.is_dir():
        return reserved + [f"{FLUX_DIR}/ is missing"]
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
            found += _kustomization_problems(flux, name, suffixes.get(name, []))
    found += [
        f"{CLUSTER_DIR}/{name}/ is missing: {name} is published and renders no directory"
        for name in sorted(names)
        if not (out / CLUSTER_DIR / name / KUSTOMIZATION).is_file()
    ] + [
        f"{CLUSTER_DIR}/{tag}{suffix}/ is missing: {tag}'s inputs call for {tag}{suffix} "
        "and it renders no such directory"
        for tag in sorted(published)
        for suffix in suffixes[tag]
        if not (out / CLUSTER_DIR / f"{tag}{suffix}" / KUSTOMIZATION).is_file()
    ]
    return reserved + found + _digests_problems(out, names)


def report(out: Path, published: list[str], roles: Path = K8S_ROLES) -> int:
    """Print the census's and the gates' problems, or one line saying both passed.

    Returns the exit code.
    """
    deferring, problems = deferring_tags(published, roles)
    snapshotting, unjudged = snapshot_tags(published, roles)
    problems += unjudged
    problems += census(out, published, deferring=deferring, snapshotting=snapshotting)
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
