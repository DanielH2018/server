"""Which callers a change to the standalone guard map reaches: the rows that moved (#5571).

`manifests/tasks/main.yml` reads only `.get(manifests_service, [])` of the map, so a row that
changed can change no other caller's deploy. Without this rule `narrow` mapped the map like
any other `manifests` path, to every role that includes `manifests`, and tripped its own
fleet-coverage ceiling. Measured on the H2.5b branch: one uptime-kuma `netpol_from` edit plus
one configarr row printed `cannot narrow (58 of 61 services ...)`, where the range reached
`configarr,netpol-baseline,uptime-kuma`.

`land_shared.guard_map_rows` asks the same question for a landing, and the deployer's owed
line asked it per caller until D6.12 deleted that discharge (#5106). This leaf cannot import
the landing's copy: `land_shared` reaches `narrow_lib.broad` through `deploy_lib.tags`, so the
edge would close a cycle. `test_land_shared_guard_map.py` holds the two paths equal.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_guard_map.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

import json
from collections.abc import Callable, Iterable
from pathlib import Path

from lib.narrow_git import CannotNarrow, changed_mapping_keys, show_at

MANIFESTS = "manifests"
GUARD_MAP = "ansible/roles/k8s/manifests/files/standalone_secret_keys.json"


def _rows_at(ref: str, cwd: Path) -> dict | None:
    """The guard map at `ref` as a dict of rows, or None when it is absent or unparseable."""
    try:
        text = show_at(ref, GUARD_MAP, cwd)
    except CannotNarrow:
        return None
    if text is None:
        return None
    try:
        rows = json.loads(text)
    except ValueError:
        return None
    return rows if isinstance(rows, dict) else None


def split_off(
    changed: Iterable[str],
    cs,
    old_ref: str,
    ctx,
    role_tags: Callable[..., set[str]],
) -> set[str]:
    """The `manifests` callers whose guard-map row moved, taking `manifests` out of `cs`.

    When the map is the range's only non-test `manifests` path, this discards `manifests`
    from `cs.k8s`, so `broad._changed_half` no longer fans it out to every caller, and
    returns the callers whose row differs. A row is keyed by `manifests_service`, so a moved
    row counts only when its key is one of the `manifests` callers. An empty set is a real
    answer: a reserialised map, or a row removed for a retired role, deploys no caller.

    Otherwise it leaves `cs` alone and returns an empty set, keeping the full fan-out: when
    the map is not in `changed`, when another non-test `manifests` path is, or when either
    side is absent or not a JSON object.

    Args:
        changed: the range's changed paths.
        cs: the range's `ChangeSet`, from `reach`.
        old_ref: the commit the checkout is on; `ctx.ref` is the other end.
        ctx: `broad.Context`, passed in rather than imported, since `broad` imports this.
        role_tags: `broad._role_tags`, which maps `manifests` to its callers' tags.
    """
    # Lazy because `reach` imports `deploy_logic` at module scope, as `broad.narrow` notes.
    from deploy_tools.reach import is_role_test_path, role_for

    changed = list(changed)
    if GUARD_MAP not in changed:
        return set()
    if any(
        role_for(p) == MANIFESTS and not is_role_test_path(p) and p != GUARD_MAP
        for p in changed
    ):
        return set()
    before, after = _rows_at(old_ref, ctx.cwd), _rows_at(ctx.ref, ctx.cwd)
    if before is None or after is None:
        return set()
    cs.k8s.discard(MANIFESTS)
    moved = changed_mapping_keys(before, after, GUARD_MAP)
    tags = moved & role_tags({MANIFESTS}, ctx) if moved else set()
    ctx.explain(f"narrow: {GUARD_MAP} -> {','.join(sorted(tags)) or '(nothing)'}")
    return tags
