"""`probe.py flux [tag...]` — each Flux Kustomization's Ready, applied commit and suspension.

Sessions read this by hand with `kubectl get kustomization` about ten times a day (#6322),
because `health <tag>` reads a Kustomization only to gate a CronJob and `releases` shows the
render commit rather than what Flux applied.

One kubectl read lists every Kustomization and OCIRepository in `flux-system`. Each row prints:

- Ready, with the condition's reason;
- the commit `status.lastAppliedOriginRevision` names, against the commit the Kustomization's
  OCIRepository serves (`deploy_tools/kustomizations.py:origin_commit` reads the first);
- whether `spec.suspend` is set.

`flux <tag>` prints the tag, each per-tag Kustomization it has (`<tag>-deferred`, `-snapshot`,
`-prepull`, `-precheck`, `-verify`) and the shared `soak`. With no tag it prints every
Kustomization. It exits 1 when a printed row is not Ready, is suspended or applied a commit
other than the one its source serves, and when a named tag has no Kustomization.

A row that is not Ready for a reason in `NOT_YET_REASONS` is mid-reconcile, not failed
(#6337). Examples are `<tag>` at Ready=Unknown/Progressing, and its `-verify` sibling at
Ready=False/DependencyNotReady while it waits on `<tag>`. Such a row prints under PENDING and
does not by itself fail the probe, whatever commit it has applied so far. The exception is a
DependencyNotReady row whose `spec.dependsOn` names a Kustomization that failed. Flux holds
that row until someone fixes the dependency, so it fails even when the dependency is not
printed (#6340). A caller that needs
the row settled waits on it with `cc-wait flux <name>`. Any other not-Ready row exits 1,
including one with no Ready condition at all, which is what a stopped kustomize-controller
leaves on an object it never reconciled.

`.claude/wait-sources/flux` runs `scripts/diagnostics/flux_wait.py`, which reads the same list
through `read_flux` and judges one Kustomization with `wait_state`.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import subprocess
from dataclasses import dataclass

from deploy_tools.kustomizations import (
    REVISION,
    is_ready,
    kustomization_name,
    origin_commit,
)
from lib.exit_codes import PROBE_UNREADABLE
from lib.git import git
from lib.kubectl import DEFAULT_CLUSTER, MissingKubectl, kubectl_argv, kubectl_json

FLUX_NAMESPACE = "flux-system"
KUSTOMIZATION = "Kustomization"
OCIREPOSITORY = "OCIRepository"
FLUX_KINDS = "kustomizations.kustomize.toolkit.fluxcd.io,ocirepositories.source.toolkit.fluxcd.io"

# `probe.py flux`'s help as `cli_parser._verdict_help` takes it (summary, exit 0, exit 1). It
# lives here because `cli_parser.py` sits at its module-length cap.
FLUX_HELP = (
    "each Flux Kustomization in flux-system: Ready with its reason, the commit "
    "lastAppliedOriginRevision names against the commit its OCIRepository serves, and "
    "whether it is suspended. A tag brings its -deferred/-snapshot/-prepull/-precheck/"
    "-verify Kustomizations and the shared soak; no tag lists every one. "
    "`cc-wait flux <name>` waits on one",
    "every row is Ready at its source's commit",
    "one is not Ready and not reconciling, is suspended, lags its source while Ready or a "
    "tag has none; a row reconciling or waiting on a dependency prints under PENDING",
)
FLUX_TAGS_HELP = "deploy tags, as `./scripts/deploy.sh --tags` takes them"

# The per-tag kinds the renderer writes beside `<tag>`. This module runs under the cc-wait
# source with no project environment, so it cannot import
# `scripts/renderer/renderer_lib/ordering.py:TAG_KIND_SUFFIXES`, which needs PyYAML.
# `scripts/diagnostics/tests/test_probe_flux.py` pins the two equal.
TAG_KIND_SUFFIXES = ("-deferred", "-snapshot", "-prepull", "-precheck", "-verify")
# The shared Kustomization every promotion that holds a tag also waits on.
SOAK = "soak"

# `flux push artifact --revision master@sha1:<sha>` writes the commit under this annotation,
# and source-controller copies it into the OCIRepository's `status.artifact.metadata`.
# `promote_state.artifact_of` reads the same field, but it is not reachable without the venv.
REVISION_ANNOTATION = "org.opencontainers.image.revision"

# Ready reasons that mean "not yet", not "failed": kustomize-controller is waiting on a
# dependency or is mid-apply. Flux sets Progressing with Ready=Unknown and DependencyNotReady
# with Ready=False. Every other reason on a not-Ready row is a failed apply or health check.
# `wait_state` and `format_flux` both read this set, so the probe and cc-wait agree.
NOT_YET_REASONS = frozenset(
    {"DependencyNotReady", "Progressing", "ProgressingWithRetry"}
)


class FluxUnreadable(Exception):
    """The Flux object list could not be read."""


@dataclass(frozen=True)
class Row:
    """One Kustomization as this probe reports it.

    Attributes:
        name: `metadata.name`.
        ready: the Ready condition's status, `True`, `False` or `Unknown` (also when absent).
        reason: the Ready condition's reason, or '' when absent.
        message: the Ready condition's message, or ''.
        applied: the commit `lastAppliedOriginRevision` names, or None.
        source: the OCIRepository `spec.sourceRef` names.
        served: the commit that OCIRepository serves, or None when it is missing or unnamed.
        attempted_served: True when `lastAttemptedRevision` equals the source's artifact
            revision, so a Ready=False is about the commit the source serves.
        suspended: `spec.suspend` is true.
        observed: `status.observedGeneration` has caught up with `metadata.generation`.
        depends_on: the names `spec.dependsOn` lists.
    """

    name: str
    ready: str
    reason: str
    message: str
    applied: str | None
    source: str
    served: str | None
    attempted_served: bool
    suspended: bool
    observed: bool
    depends_on: tuple[str, ...] = ()

    @property
    def settled(self) -> bool:
        """Ready, not suspended, and applied the commit its source serves."""
        return (
            self.ready == "True"
            and not self.suspended
            and self.applied is not None
            and self.applied == self.served
        )


def _ready_condition(item: dict) -> dict:
    status = item.get("status")
    conditions = status.get("conditions") if isinstance(status, dict) else None
    for condition in conditions if isinstance(conditions, list) else []:
        if isinstance(condition, dict) and condition.get("type") == "Ready":
            return condition
    return {}


def served(ocirepository: dict | None) -> tuple[str | None, str]:
    """The commit an OCIRepository serves and its artifact revision (`prod@sha256:...`)."""
    status = (ocirepository or {}).get("status")
    artifact = status.get("artifact") if isinstance(status, dict) else None
    artifact = artifact if isinstance(artifact, dict) else {}
    metadata = artifact.get("metadata")
    origin = metadata.get(REVISION_ANNOTATION) if isinstance(metadata, dict) else None
    match = REVISION.fullmatch(origin) if isinstance(origin, str) else None
    revision = artifact.get("revision")
    return (match.group(1) if match else None), (
        revision if isinstance(revision, str) else ""
    )


def _dict(value) -> dict:
    """`value` when it is a dict, else an empty one: the list comes from the cluster."""
    return value if isinstance(value, dict) else {}


def row(item: dict, sources: dict[str, dict]) -> Row:
    """The `Row` of Kustomization `item`, judged against the sources by name."""
    condition = _ready_condition(item)
    spec = _dict(item.get("spec"))
    status = _dict(item.get("status"))
    metadata = _dict(item.get("metadata"))
    name = _dict(spec.get("sourceRef")).get("name")
    source = name if isinstance(name, str) else ""
    commit, revision = served(sources.get(source))
    attempted = status.get("lastAttemptedRevision")
    depends = spec.get("dependsOn")
    depends_on = tuple(
        d["name"]
        for d in (depends if isinstance(depends, list) else [])
        if isinstance(d, dict) and isinstance(d.get("name"), str)
    )
    return Row(
        name=kustomization_name(item),
        ready="True" if is_ready(item) else str(condition.get("status") or "Unknown"),
        reason=str(condition.get("reason") or ""),
        message=str(condition.get("message") or ""),
        applied=origin_commit(item),
        source=source,
        served=commit,
        attempted_served=bool(revision) and attempted == revision,
        suspended=spec.get("suspend") is True,
        observed=status.get("observedGeneration") == metadata.get("generation"),
        depends_on=depends_on,
    )


def split(items: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """The Kustomizations, and the OCIRepositories by name, of one listing."""
    kustomizations = [i for i in items if i.get("kind") == KUSTOMIZATION]
    sources = {
        kustomization_name(i): i for i in items if i.get("kind") == OCIREPOSITORY
    }
    return kustomizations, sources


def flux_args() -> list[str]:
    """The one read, as kubectl arguments, so `--dry-run` prints exactly what runs."""
    return ["-n", FLUX_NAMESPACE, "get", FLUX_KINDS, "-o", "json"]


def read_flux(cluster: str = DEFAULT_CLUSTER, read=kubectl_json) -> list[dict]:
    """Every Kustomization and OCIRepository in `flux-system`.

    Raises:
      FluxUnreadable: kubectl failed, timed out or is missing, or listed no Kustomization at all. Flux
        always holds the root `homelab`, so an empty list is a failed read, never "nothing
        to report".
      lib.kubectl.WrongCluster: the local kubectl serves another cluster.
    """
    try:
        doc = read(cluster, *flux_args())
    except MissingKubectl as exc:
        raise FluxUnreadable(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise FluxUnreadable(f"kubectl timed out after {exc.timeout}s") from exc
    items = (doc or {}).get("items")
    if not isinstance(items, list):
        raise FluxUnreadable(f"kubectl {' '.join(flux_args())} returned no list")
    items = [i for i in items if isinstance(i, dict)]
    if not any(i.get("kind") == KUSTOMIZATION for i in items):
        raise FluxUnreadable(f"no Kustomization in {FLUX_NAMESPACE}")
    return items


def select(names: list[str], tags: list[str]) -> tuple[list[str], list[str]]:
    """The Kustomization names `flux <tag...>` prints, and the tags that have none.

    A tag brings its per-tag kinds and `soak`. No tag selects every name.
    """
    if not tags:
        return sorted(names), []
    present = set(names)
    chosen: list[str] = []
    missing = []
    for tag in tags:
        if tag not in present:
            missing.append(tag)
        for name in [tag, *(tag + suffix for suffix in TAG_KIND_SUFFIXES)]:
            if name in present and name not in chosen:
                chosen.append(name)
    if SOAK in present and SOAK not in chosen:
        chosen.append(SOAK)
    return chosen, missing


def _short(commit: str | None) -> str:
    return commit[:8] if commit else "-"


def failed_dependency(rows: dict[str, Row], name: str, seen=frozenset()) -> str | None:
    """The dependency that holds `name` at DependencyNotReady for good, or None.

    A dependency has failed when it is not Ready for a reason outside `NOT_YET_REASONS`, or
    when a failed dependency of its own holds it. A dependency the listing lacks does not
    count, nor does one already seen on a cycle.
    """
    for dep in rows[name].depends_on:
        d = rows.get(dep)
        if d is None or dep in seen or d.ready == "True":
            continue
        if d.reason not in NOT_YET_REASONS:
            return dep
        if d.reason == "DependencyNotReady" and failed_dependency(
            rows, dep, seen | {name}
        ):
            return dep
    return None


def _root_failure(rows: dict[str, Row], dep: str) -> Row:
    """The row whose failure holds `dep`, the dependency `failed_dependency` named.

    That is `dep` itself unless `dep` is DependencyNotReady, in which case the chain is
    followed down to the dependency that failed.
    """
    seen = set()
    while rows[dep].reason == "DependencyNotReady" and dep not in seen:
        seen.add(dep)
        nxt = failed_dependency(rows, dep)
        if nxt is None:
            break
        dep = nxt
    return rows[dep]


def format_flux(items: list[dict], tags: list[str]) -> tuple[str, int]:
    """The table and the verdict for `flux <tags>`, as (text, exit code)."""
    kustomizations, sources = split(items)
    rows = {r.name: r for r in (row(k, sources) for k in kustomizations)}
    chosen, missing = select(list(rows), tags)
    width = max((len(n) for n in chosen), default=4)
    lines = [
        f"  {'NAME':<{width}}  READY    {'REASON':<26} APPLIED   SOURCE    SUSPENDED"
    ]
    for name in chosen:
        r = rows[name]
        lines.append(
            f"  {name:<{width}}  {r.ready:<8} {r.reason[:26]:<26} {_short(r.applied):<9} "
            f"{_short(r.served):<9} {'yes' if r.suspended else 'no'}"
        )
    lines.append("")
    problems = [f"{t}: no Kustomization (not Flux-owned?)" for t in missing]
    pending = []
    for name in chosen:
        r = rows[name]
        if r.settled:
            continue
        if r.suspended:
            problems.append(f"{name}: suspended")
        elif r.reason == "DependencyNotReady" and (
            dep := failed_dependency(rows, name)
        ):
            problems.append(
                f"{name}: Ready={r.ready} {r.reason}: dependency {dep} failed"
            )
        elif r.ready != "True" and r.reason in NOT_YET_REASONS:
            pending.append(f"{name}: Ready={r.ready} {r.reason}: {r.message[:200]}")
        elif r.ready != "True":
            problems.append(f"{name}: Ready={r.ready} {r.reason}: {r.message[:200]}")
        else:
            problems.append(
                f"{name}: applied {_short(r.applied)}, but {r.source or 'its source'} "
                f"serves {_short(r.served)}"
            )
    if pending:
        lines.append("PENDING (reconciling, not failed):")
        lines.extend(f"  {p}" for p in pending)
    if problems:
        lines.append("FAIL:")
        lines.extend(f"  {p}" for p in problems)
        return "\n".join(lines), 1
    if pending:
        lines.append(
            f"OK: {len(chosen) - len(pending)} of {len(chosen)} Kustomizations are Ready at "
            f"the commit their source serves; {len(pending)} still reconciling."
        )
        return "\n".join(lines), 0
    lines.append(
        f"OK: all {len(chosen)} Kustomizations are Ready at the commit their source serves."
    )
    return "\n".join(lines), 0


# The checkout `contains` asks: the one this file sits in, which shares its object store with
# every worktree of the repo.
REPO = _Path(__file__).resolve().parents[3]


def contains(commit: str, ancestor: str) -> bool:
    """Whether `commit` is `ancestor` or descends from it, by this checkout's git history.

    False when git does not know either commit, so an unfetched promotion keeps a wait open
    rather than ending it.
    """
    try:
        proc = git(
            "merge-base",
            "--is-ancestor",
            ancestor,
            commit,
            cwd=REPO,
            check=False,
            timeout=10,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def wait_state(
    items: list[dict], name: str, expected: str | None, contains=contains
) -> dict:
    """One cc-wait reading of Kustomization `name`: `{"state", "detail"}`.

    `expected` is a commit or a prefix of one; None means the commit the source serves.
    With no `expected`, `ready` needs the applied commit to equal the served one. After master
    is reset to an older commit, the applied commit still descends from it but is stale
    (#6336). An explicit `expected` is `ready` once the applied commit is it or descends from
    it, because a later promotion can land before Flux applies `expected` and carry its
    change. `failed`
    requires the source to serve `expected` itself, so a failure at another commit keeps the
    wait open rather than ending it.

    A DependencyNotReady row is `failed` when `failed_dependency` names a dependency, because
    Flux holds it there until someone fixes that dependency (#6354). The dependency at the end
    of that chain must have attempted the artifact its source serves, so a failure left over
    from an older commit keeps the wait open.

    Args:
        contains: `contains`'s signature, injectable so a test needs no git history.
    """
    kustomizations, sources = split(items)
    rows = {r.name: r for r in (row(k, sources) for k in kustomizations)}
    if name not in rows:
        return {
            "state": "absent",
            "detail": f"no Kustomization {name} in {FLUX_NAMESPACE}",
        }
    r = rows[name]
    want = expected or r.served
    if r.suspended:
        return {
            "state": "suspended",
            "detail": f"{name} is suspended; it applies nothing",
        }
    if not want:
        return {
            "state": "waiting",
            "detail": f"{r.source or 'the source'} serves no commit yet",
        }
    if expected is None:
        at_want = r.applied is not None and r.applied == r.served
    else:
        at_want = r.applied is not None and (
            r.applied.startswith(want) or contains(r.applied, want)
        )
    if r.observed and r.ready == "True" and at_want:
        return {
            "state": "ready",
            "detail": f"{name} is Ready at {_short(r.applied)}, which holds {_short(want)}",
        }
    source_at_want = r.served is not None and r.served.startswith(want)
    if (
        r.observed
        and r.ready == "False"
        and r.reason not in NOT_YET_REASONS
        and source_at_want
        and r.attempted_served
    ):
        return {
            "state": "failed",
            "detail": f"{name} failed at {_short(r.served)}: {r.reason}: {r.message[:300]}",
        }
    dep = (
        failed_dependency(rows, name)
        if r.observed
        and r.ready == "False"
        and r.reason == "DependencyNotReady"
        and source_at_want
        else None
    )
    root = _root_failure(rows, dep) if dep else None
    if root is not None and root.attempted_served:
        return {
            "state": "failed",
            "detail": f"{name} is held at DependencyNotReady: dependency {dep} failed"
            + (f" (held by {root.name})" if root.name != dep else "")
            + f": {root.reason}: {root.message[:300]}",
        }
    return {
        "state": "progressing",
        "detail": f"{name} Ready={r.ready} {r.reason}, applied {_short(r.applied)}, "
        f"waiting for {_short(want)}",
    }


def run_flux(ns, read=kubectl_json) -> int:
    """Print each selected Kustomization and the verdict.

    Args:
        ns: the parsed `probe.py flux` arguments.
        read: `lib.kubectl.kubectl_json` or a stand-in with its signature.
    """
    if getattr(ns, "dry_run", False):
        print(" ".join(kubectl_argv(*flux_args())))
        return 0
    try:
        items = read_flux(getattr(ns, "cluster", DEFAULT_CLUSTER), read=read)
    except FluxUnreadable as exc:
        print(f"flux: {exc}", file=_sys.stderr)
        return PROBE_UNREADABLE
    text, code = format_flux(items, list(getattr(ns, "tags", None) or []))
    print(text)
    return code
