"""List the managed fields a Flux takeover strips or leaves behind, from two JSON inputs (H1.1j).

The operator runs this before each H1 flip (`docs/target-architecture/host/h1-cluster-config-to-flux.md`,
*A takeover census before each flip*). It reads two inputs and makes no API call of its own:

- LIVE: the tag's live objects, from `get -o json --show-managed-fields`.
- DRY-RUN: a server-side dry-run apply of the render as `kustomize-controller` with
  `--force-conflicts`, as JSON.

Either input is one object or a `List`, and either may be `-` for standard input. Objects pair
by API group, kind, namespace and name.

The dry run's `kustomize-controller` Apply entry names exactly the fields the render declares.
The census reads "declared" from that entry and never from the dry-run body: the dry run skips
kustomize-controller's cleanup, so its body still holds every live field. For each object the
census flattens each `fieldsV1` tree into leaf paths and prints, by set difference:

- `removed`: paths a cleaned manager owns, no other manager owns and the render does not
  declare. The apply at the flip deletes each one. `last-applied-configuration` is the one
  expected entry and is marked so.
- `kept`: paths a manager outside the cleanup list owns and the render does not declare. They
  stay live after the flip, invisible to Flux. `status` paths are left out.
- `rolls`: a Deployment, DaemonSet or StatefulSet with a `removed` path under its pod template,
  such as `kubectl.kubernetes.io/restartedAt` under `kubectl-rollout`.

A path only `kubectl-client-side-apply` owns that its last-applied config omits is a field the
server defaulted at the create. It prints apart as a presumed default and does not set `rolls`,
as `probe.py takeover` does: the server defaults it again to the same value.

The cleanup list, the flattening and the default rule are D5.1g's, imported from
`diagnostics/probe_lib/takeover.py`, so the two censuses cannot drift apart. That module's
comment names the upstream source: kustomize-controller's
`internal/controller/kustomization_controller.go` for the four entries, and fluxcd/pkg
`ssa/patch.go:matchFieldManager` for the prefix and subresource rules.

It prints field paths and manager names only, never a value, because the inputs can hold
Secret data.

Exit codes: 0 the census printed; 1 an input is unreadable, the two inputs share no object, or
a dry-run object carries no `kustomize-controller` Apply entry; 64 a usage error. An empty match
proves nothing, so it never passes.
"""

# `diagnostics` lives beside `dev` under `scripts/`, and a directly-run script gets only its own
# directory on sys.path. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import json
import sys

from diagnostics.probe_lib.takeover import (
    LAST_APPLIED_PATH,
    format_path,
    is_cleaned,
    leaf_paths,
    rolls,
    server_defaulted,
)
from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK

FLUX_MANAGER = "kustomize-controller"
STATUS = ("f", "status")


class CensusError(Exception):
    """An input the census cannot trust, so it prints no result."""


def is_flux_apply(entry):
    """True for kustomize-controller's own Apply entry: the render, not a field to hand over."""
    return entry.get("manager") == FLUX_MANAGER and entry.get("operation") == "Apply"


def objects(doc):
    """The objects in one parsed input: the items of a `List`, or the document itself."""
    if isinstance(doc, dict) and isinstance(doc.get("items"), list):
        return [item for item in doc["items"] if isinstance(item, dict)]
    if isinstance(doc, dict):
        return [doc]
    raise CensusError("an input is neither a Kubernetes object nor a List")


def key(obj):
    """`(group, kind, namespace, name)`, which pairs a live object with its dry run."""
    meta = obj.get("metadata") or {}
    group = (obj.get("apiVersion") or "").rpartition("/")[0]
    return (
        group,
        obj.get("kind") or "",
        meta.get("namespace") or "",
        meta.get("name") or "",
    )


def label(obj_key):
    """`<Kind> <namespace>/<name>`, without a namespace when the object has none."""
    _, kind, namespace, name = obj_key
    return f"{kind} {namespace}/{name}" if namespace else f"{kind} {name}"


def _entries(obj):
    return (obj.get("metadata") or {}).get("managedFields") or []


def declared_paths(dryrun):
    """The leaf paths the render declares: the dry run's kustomize-controller Apply entry."""
    applies = [e for e in _entries(dryrun) if is_flux_apply(e)]
    if not applies:
        raise CensusError(
            f"{label(key(dryrun))}: the dry-run object has no {FLUX_MANAGER} Apply entry, so it "
            f"was not applied with --field-manager={FLUX_MANAGER} and declares nothing"
        )
    return set().union(*(leaf_paths(e.get("fieldsV1")) for e in applies))


def compare(live, dryrun):
    """`(removed, kept, defaulted)` for one object pair.

    `removed` maps each path to the cleaned managers that own it. An entry on a subresource is
    neither cleaned nor kept, as `matchFieldManager` skips it and the render never writes there.
    A live kustomize-controller Apply entry is Flux's own last apply, which the next one replaces.
    """
    declared = declared_paths(dryrun)
    cleaned, other = {}, set()
    for entry in _entries(live):
        if entry.get("subresource") or is_flux_apply(entry):
            continue
        paths = leaf_paths(entry.get("fieldsV1"))
        if is_cleaned(entry):
            for path in paths:
                cleaned.setdefault(path, set()).add(entry.get("manager"))
        else:
            other |= paths
    removed = {
        path: sorted(managers)
        for path, managers in cleaned.items()
        if path not in other and path not in declared
    }
    kept = {path for path in other - declared if path[:1] != (STATUS,)}
    # The default rule ignores last-applied itself; it stays an expected `removed` entry.
    defaulted = server_defaulted(
        {p: m for p, m in removed.items() if p != LAST_APPLIED_PATH}, live, dryrun
    )
    return {p: m for p, m in removed.items() if p not in defaulted}, kept, defaulted


def census(live_objs, dryrun_objs):
    """The census text for two object lists.

    Raises:
        CensusError: the lists share no object, so there is nothing to compare.
    """
    live_by_key = {key(obj): obj for obj in live_objs}
    dry_by_key = {key(obj): obj for obj in dryrun_objs}
    shared = sorted(live_by_key.keys() & dry_by_key.keys())
    if not shared:
        raise CensusError(
            "the live and dry-run inputs share no object, so the census proves nothing"
        )
    lines = []
    for only, where in (
        (
            dry_by_key.keys() - live_by_key.keys(),
            "not live, so the takeover creates it",
        ),
        (
            live_by_key.keys() - dry_by_key.keys(),
            "in the live input only, so not checked",
        ),
    ):
        lines += [f"{label(k)}: {where}" for k in sorted(only)]
    removed_total = kept_total = 0
    rolling = []
    for obj_key in shared:
        removed, kept, defaulted = compare(live_by_key[obj_key], dry_by_key[obj_key])
        flag = rolls(obj_key[1], removed)
        lines.append(f"{label(obj_key)}:{' rolls' if flag else ''}")
        if flag:
            rolling.append(label(obj_key))
        lines.append(f"  removed ({len(removed)}):")
        for path in sorted(removed, key=format_path):
            note = ", expected" if path == LAST_APPLIED_PATH else ""
            lines.append(f"    {format_path(path)}  ({', '.join(removed[path])}{note})")
        lines.append(f"  kept ({len(kept)}):")
        lines += [f"    {format_path(p)}" for p in sorted(kept, key=format_path)]
        if defaulted:
            lines.append(
                f"  presumed server defaults, inferred and not measured ({len(defaulted)}):"
            )
            lines += [
                f"    {format_path(p)}" for p in sorted(defaulted, key=format_path)
            ]
        removed_total += len(removed)
        kept_total += len(kept)
    lines.append(
        f"{removed_total} removed and {kept_total} kept across {len(shared)} object(s); "
        f"{len(rolling)} workload(s) roll"
        + (f": {', '.join(rolling)}" if rolling else "")
    )
    return "\n".join(lines)


def _read(arg, stdin):
    """One input's objects. The error never quotes the input, which can hold Secret data."""
    try:
        text = stdin.read() if arg == "-" else _Path(arg).read_text()
        return objects(json.loads(text))
    except (OSError, json.JSONDecodeError) as exc:
        name = "standard input" if arg == "-" else arg
        raise CensusError(
            f"{name} is not readable JSON ({type(exc).__name__})"
        ) from None


def main(argv=None, stdin=None):
    parser = ArgumentParser(
        description="List the fields a Flux takeover removes or leaves behind (H1.1j).",
    )
    parser.add_argument(
        "live", help="live objects as JSON, with managed fields; - for stdin"
    )
    parser.add_argument(
        "dryrun", help=f"the dry-run apply as {FLUX_MANAGER}, as JSON; - for stdin"
    )
    args = parser.parse_args(argv)
    if args.live == args.dryrun == "-":
        parser.error("only one input can be standard input")
    try:
        text = census(
            _read(args.live, stdin or sys.stdin), _read(args.dryrun, stdin or sys.stdin)
        )
    except CensusError as exc:
        print(f"takeover_census: {exc}", file=sys.stderr)
        return FAILED
    print(text)
    return OK


if __name__ == "__main__":
    sys.exit(main())
