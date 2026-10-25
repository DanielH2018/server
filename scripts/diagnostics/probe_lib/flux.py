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

`.claude/wait-sources/flux` runs `scripts/diagnostics/flux_wait.py`, which reads the same list
through `read_flux` and judges one Kustomization with `wait_state`.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dataclasses import dataclass

from deploy_tools.kustomizations import (
    REVISION,
    is_ready,
    kustomization_name,
    origin_commit,
)
from lib.exit_codes import PROBE_UNREADABLE
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
    "one is not Ready, is suspended, lags its source or a tag has none",
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

# Ready=False reasons that mean "not yet", not "failed": kustomize-controller is waiting on a
# dependency or is mid-apply. Every other False reason is a failed apply or health check.
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
      FluxUnreadable: kubectl failed or is missing, or listed no Kustomization at all. Flux
        always holds the root `homelab`, so an empty list is a failed read, never "nothing
        to report".
      lib.kubectl.WrongCluster: the local kubectl serves another cluster.
    """
    try:
        doc = read(cluster, *flux_args())
    except MissingKubectl as exc:
        raise FluxUnreadable(str(exc)) from exc
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
    for name in chosen:
        r = rows[name]
        if r.settled:
            continue
        if r.suspended:
            problems.append(f"{name}: suspended")
        elif r.ready != "True":
            problems.append(f"{name}: Ready={r.ready} {r.reason}: {r.message[:200]}")
        else:
            problems.append(
                f"{name}: applied {_short(r.applied)}, but {r.source or 'its source'} "
                f"serves {_short(r.served)}"
            )
    if problems:
        lines.append("FAIL:")
        lines.extend(f"  {p}" for p in problems)
        return "\n".join(lines), 1
    lines.append(
        f"OK: all {len(chosen)} Kustomizations are Ready at the commit their source serves."
    )
    return "\n".join(lines), 0


def wait_state(items: list[dict], name: str, expected: str | None) -> dict:
    """One cc-wait reading of Kustomization `name`: `{"state", "detail"}`.

    `expected` is a commit or a prefix of one; None means the commit the source serves.
    `ready` and `failed` both require that commit to be the one in question, so a failure at
    an earlier commit keeps the wait open rather than ending it.
    """
    kustomizations, sources = split(items)
    match = [k for k in kustomizations if kustomization_name(k) == name]
    if not match:
        return {
            "state": "absent",
            "detail": f"no Kustomization {name} in {FLUX_NAMESPACE}",
        }
    r = row(match[0], sources)
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
    at_want = r.applied is not None and r.applied.startswith(want)
    if r.observed and r.ready == "True" and at_want:
        return {"state": "ready", "detail": f"{name} is Ready at {_short(r.applied)}"}
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
