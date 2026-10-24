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
  `k8s_autodeploy_snapshot_pvcs`, `-prepull` when it sets `flux_prepull_timeout`,
  `-precheck` when it ships `templates/precheck/` (#5719), and `-verify` when it ships
  `templates/verify/`;
- each published tag and each shared kind has its directory `cluster/<name>/`, and each
  per-tag kind its directory `cluster/<name><suffix>/`, each with a `kustomization.yaml`;
- no published tag takes a reserved name (`reserved_tags`), such as a shared kind's
  `homelab-namespace`;
- `digests.json` names each published tag and shared kind, plus `_unclassified`.

Beside it, the per-tag object census (`member_census`, H1.1c) fails a published tag whose
render drops one of its `NAMED_MEMBERS`. A plumbing tag sets `prune: false`, so a dropped
object stays serving and nothing reports it; the named members are the objects whose loss
matters most. `longhorn-restore-drill` prunes, and its one member, the stamp ConfigMap, carries
`prune: disabled`, so it too stays live and nothing reports it (H2.3b). The census applies to a
tag from the commit that publishes it, so a tag the bundle does not publish yet is not judged.

The allowlist comes from the renderer's inputs, never from its output, so a renderer bug
cannot widen it: the tags the inventory marks `owner: flux` (`published_tags`), the literal
`SHARED_KINDS`, and the published tags whose role passes `manifests_deferred_files` to its
`k8s/manifests` include (`deferring_tags`), read as the renderer reads the include, and those
whose role declares snapshot claims (`snapshot_tags`), those whose role sets
`flux_prepull_timeout` (`prepull_tags`), those whose role ships pre-apply check Jobs as
`templates/precheck/*.yaml.j2` (`prechecking_tags`), and those whose role ships verify or post-apply
mutation Jobs as `templates/verify/*.yaml.j2` (`verifying_tags`) or opts a CronJob into a gate
Job with `flux_cronjob_gates` (`gating_tags`). A tag marked
`owner: released` is left out of both the bundle and the allowlist (C4).

The plan is docs/target-architecture/deploy/d2-first-leaf.md, *Publication and verification*,
and docs/target-architecture/host/h1-cluster-config-to-flux.md, *Plumbing tags are never pruned*,
for the member census.

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
from lib.k8s_roles import snapshot_claims, tag_owners
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
    PRECHECK_SUFFIX,
    PREPULL_SUFFIX,
    SNAPSHOT_SUFFIX,
    TAG_KIND_SUFFIXES,
    VERIFY_SUFFIX,
    cronjob_gates,
    precheck_files,
    prepull_timeouts,
    verify_files,
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
    `k8s_autodeploy_snapshot_pvcs`, which alone calls for `<tag>-snapshot` (D5.2a). It reads
    `snapshot_claims`, the reader `digests.json`'s `claims` comes from too.
    """
    claims, problems = snapshot_claims(published, roles)
    return frozenset(tag for tag, names in claims.items() if names), problems


def prepull_tags(
    published: Iterable[str], roles: Path = K8S_ROLES
) -> tuple[frozenset[str], list[str]]:
    """The published tags whose role sets `flux_prepull_timeout`, and one line per problem.

    That default alone calls for `<tag>-prepull` (#5525). It reads `prepull_timeouts`, the
    reader the renderer reads too.
    """
    timeouts, problems = prepull_timeouts(published, roles)
    return frozenset(timeouts), problems


def gating_tags(
    published: Iterable[str], roles: Path = K8S_ROLES
) -> tuple[frozenset[str], list[str]]:
    """The published tags whose role sets `flux_cronjob_gates`, and one line per problem.

    That default calls for `<tag>-verify`, which holds the gate Jobs (`cronjob_gate.py`). It
    reads `cronjob_gates`, the reader the renderer reads too.
    """
    gates, problems = cronjob_gates(published, roles)
    return frozenset(gates), problems


def prechecking_tags(
    published: Iterable[str], roles: Path = K8S_ROLES
) -> frozenset[str]:
    """The published tags whose role ships pre-apply check Jobs as `templates/precheck/`.

    That directory alone calls for `<tag>-precheck` (#5719). It reads `precheck_files`, the
    reader the renderer renders that directory from too.
    """
    return frozenset(
        tag for tag, names in precheck_files(published, roles).items() if names
    )


def verifying_tags(published: Iterable[str], roles: Path = K8S_ROLES) -> frozenset[str]:
    """The published tags whose role ships verify or post-apply mutation Jobs.

    A role ships them as `templates/verify/*.yaml.j2`, which makes the renderer write
    `cluster/<tag>-verify/` and the Kustomization `<tag>-verify` (D5.1a1). A gate Job
    (`gating_tags`) is the one other input that does. It reads
    `verify_files`, the reader the renderer renders that directory from too.
    """
    return frozenset(
        tag for tag, names in verify_files(published, roles).items() if names
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
    prepulling: Iterable[str] = (),
    prechecking: Iterable[str] = (),
) -> list[str]:
    """Every way the bundle at `out` differs from the allowlist, one line each.

    Args:
        out: The bundle root, holding `flux/`, `cluster/` and `digests.json`.
        published: The published tags, from the inventory.
        kinds: The shared kinds.
        deferring: The published tags that defer files, from their roles (`deferring_tags`).
        snapshotting: The published tags that declare snapshot claims (`snapshot_tags`).
        prepulling: The published tags whose role sets `flux_prepull_timeout`
            (`prepull_tags`).
        verifying: The published tags with a verify or post-apply mutation Job
            (`verifying_tags`).
        prechecking: The published tags with a pre-apply check Job (`prechecking_tags`).

    A shared kind has no per-tag kind.
    """
    published, kinds = set(published), set(kinds)
    granted = {
        DEFERRED_SUFFIX: set(deferring) & published,
        SNAPSHOT_SUFFIX: set(snapshotting) & published,
        PREPULL_SUFFIX: set(prepulling) & published,
        PRECHECK_SUFFIX: set(prechecking) & published,
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


# Each plumbing tag's named members, as (kind, metadata.name), from H1's *Plumbing tags are
# never pruned*, plus the restore drill's (H2.3b). Change a row in the PR that changes that list.
NAMED_MEMBERS: dict[str, frozenset[tuple[str, str]]] = {
    "cluster-base": frozenset(
        {
            ("PriorityClass", "homelab-critical"),
            ("ServiceAccount", "homelab-readonly"),
            ("Secret", "homelab-readonly-token"),
        }
    ),
    "metallb": frozenset({("DaemonSet", "speaker"), ("Deployment", "controller")}),
    "coredns": frozenset({("Deployment", "coredns"), ("Service", "kube-dns")}),
    "longhorn": frozenset({("DaemonSet", "longhorn-manager")}),
    "longhorn-backup": frozenset(
        {
            ("BackupTarget", "default"),
            ("BackupTarget", "r2"),
            ("RecurringJob", "daily-backup"),
        }
    ),
    "longhorn-reconcile": frozenset({("CronJob", "longhorn-reconcile")}),
    # Not a plumbing tag: it prunes. Its stamp ConfigMap holds state no render restores and
    # carries `prune: disabled`, so a render that drops it leaves it live (H2 Open question 12).
    "longhorn-restore-drill": frozenset(
        {("ConfigMap", "longhorn-restore-drill-stamps")}
    ),
}


def _rendered_objects(directory: Path) -> set[tuple[str, str]]:
    """The (kind, name) of each object the files `directory`'s kustomization lists hold.

    A file the kustomization does not list is not applied, so it counts for nothing. A
    missing or unparseable file counts for nothing either; `census` reports those.
    """
    docs = _documents(directory / KUSTOMIZATION) if directory.is_dir() else []
    if isinstance(docs, str) or len(docs) != 1 or not isinstance(docs[0], dict):
        return set()
    found = set()
    for resource in docs[0].get("resources") or []:
        path = directory / str(resource)
        held = _documents(path) if path.is_file() else []
        for doc in [] if isinstance(held, str) else held:
            if isinstance(doc, dict) and isinstance(doc.get("metadata"), dict):
                found.add((str(doc.get("kind")), str(doc["metadata"].get("name"))))
    return found


def _applied_directories(out: Path, tag: str) -> list[Path]:
    """The directories the Kustomizations in `flux/<tag>.yaml` apply.

    Only a directory a Kustomization names is applied, so a stray `cluster/<tag>-verify/`
    that no document names holds nothing Flux manages. A document's path outside
    `cluster/` counts for nothing; `census` reports it.
    """
    docs = _documents(out / FLUX_DIR / f"{tag}.yaml")
    found = []
    for doc in [] if isinstance(docs, str) else docs:
        spec = doc.get("spec") if isinstance(doc, dict) else None
        path = spec.get("path") if isinstance(spec, dict) else None
        if isinstance(path, str) and path.startswith(f"./{CLUSTER_DIR}/"):
            found.append(out / path.removeprefix("./"))
    return found


def member_census(
    out: Path,
    published: Iterable[str],
    members: dict[str, frozenset[tuple[str, str]]] = NAMED_MEMBERS,
) -> list[str]:
    """One line for each named member a published tag's render drops.

    A tag's render is every directory a Kustomization in `flux/<tag>.yaml` applies, so
    moving a member into `<tag>-deferred` does not drop it, and moving it into a directory
    no Kustomization applies does.
    """
    problems = []
    for tag in sorted(set(published) & set(members)):
        rendered = set().union(
            *(_rendered_objects(d) for d in _applied_directories(out, tag))
        )
        problems += [
            f"{tag} renders no {kind} {name}, a named member: a plumbing tag that drops "
            "it leaves it serving with nothing managing it"
            for kind, name in sorted(members[tag] - rendered)
        ]
    return problems


def report(out: Path, published: list[str], roles: Path = K8S_ROLES) -> int:
    """Print the census's and the gates' problems, or one line saying both passed.

    Returns the exit code.
    """
    deferring, problems = deferring_tags(published, roles)
    snapshotting, unjudged = snapshot_tags(published, roles)
    problems += unjudged
    prepulling, unjudged = prepull_tags(published, roles)
    problems += unjudged
    gating, unjudged = gating_tags(published, roles)
    problems += unjudged
    problems += census(
        out,
        published,
        deferring=deferring,
        snapshotting=snapshotting,
        verifying=verifying_tags(published, roles) | gating,
        prepulling=prepulling,
        prechecking=prechecking_tags(published, roles),
    )
    problems += member_census(out, published)
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
        f"check-bundle: the census, the member census and the source, host and no-stub "
        f"gates pass: "
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
