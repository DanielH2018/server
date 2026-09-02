"""PVC and claim accounting -- which volumes a role renders, and whether it is mid-migration.

A role that renders a PVC must declare it as a claim so the snapshot and revert roles can
protect it, and a claim token that resolves through a single-variable default has to resolve
to the same literal the manifest renders. `_migrating_state` is the shape volume-snapshot exists
for: a `strategy: Recreate` Deployment against at least one rendered RWO claim.
Consumed by `test_k8s_autodeploy_guard.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

from _autodeploy import _LITERAL_NAME, _live_tasks, _role_defaults, _strip_comments


_PVC_KIND = re.compile(
    r"^kind:\s*[\"']?PersistentVolumeClaim[\"']?\s*(?:#.*)?$\n\s*metadata:\s*$\n",
    re.MULTILINE,
)


def _pvc_names(text: str) -> list[str]:
    """`metadata.name` for every `kind: PersistentVolumeClaim` document in `text`.

    Reads the metadata block by indentation rather than assuming `name:` is the line
    immediately after `metadata:` — the earlier regex required exactly that, so a PVC whose
    metadata carried `labels:` before `name:` yielded no claim and no complaint (R6). A key at
    the same indentation as the block's first key is a sibling of `name:`; a shallower
    indentation means the metadata block ended.

    Two limits this still does not close, both real and left as read-the-tree-by-eye cases
    rather than chased into a renderer:

    - A PVC document nested inside `{% if ... %}` is credited whether or not that condition is
      ever true.
    - A `.j2` file left in the tree after being dropped from `manifests_files` (the list
      `roles/k8s/manifests` actually applies) is credited too — this glob has no notion of
      "still deployed".

    So this predicate is not fail-closed: it is fail-open in both of those shapes, and
    fail-closed only against the metadata-ordering gap R6 fixed.
    """
    names = []
    for kind_match in _PVC_KIND.finditer(text):
        indent = None
        for line in text[kind_match.end() :].splitlines():
            if not line.strip():
                continue
            this_indent = len(line) - len(line.lstrip(" \t"))
            if indent is None:
                indent = this_indent
            elif this_indent < indent:
                break
            if this_indent == indent:
                name = re.match(r"name:\s*(.+?)\s*$", line.strip())
                if name:
                    names.append(name.group(1))
                    break
    return names


# The only PVC-name shape this repo writes: a whole-field reference to exactly one role-local
# var — never a literal string, never a compound expression (`{{ a }}-{{ b }}`), never a
# filter. `_resolve_claim_token` refuses anything else rather than guessing at it.
_SINGLE_VAR_REF = re.compile(r"^\{\{\s*(\w+)\s*\}\}$")


def _resolve_claim_token(token: str, defaults: dict) -> str | None:
    """A PVC-name token, resolved to the literal claim name it renders — or None if it can't be.

    `token` is either already a literal (`_LITERAL_NAME`, this file's existing standard for "no
    Jinja left to resolve") or the single-var shape `_SINGLE_VAR_REF` matches, looked up in the
    role's OWN defaults. A var absent from defaults, a non-string value, or a value that isn't
    itself a literal (chained Jinja) all return None — the caller's job, not this function's, is
    to decide whether None means "report as unresolvable" or "count as not rendered".
    """
    token = token.strip()
    if _LITERAL_NAME.match(token):
        return token
    match = _SINGLE_VAR_REF.match(token)
    if match:
        value = defaults.get(match.group(1))
        if isinstance(value, str) and _LITERAL_NAME.match(value):
            return value
    return None


def _rendered_pvc_claims(role: Path) -> tuple[set[str], list[str]]:
    """PVC claim names `role` actually causes to exist, as `(resolved, unresolved_tokens)`.

    Two sources, because this repo builds a PVC two different ways:

    1. A `kind: PersistentVolumeClaim` document in the role's own `templates/*.j2` —
       zigbee2mqtt's data claim and code-server's workspace claim are the only two.
    2. A `vars: volume_claim_name: ...` on a task that includes `k8s/volume-claim` — how the
       other twelve claims are actually created. Read through `_live_tasks`, the same walker
       `_batch_gated_names` uses, so a commented-out or `when: false`-gated include credits
       nothing, the same "argument-against read as the thing itself" trap this file's other
       matchers are written against.

    Every token found by either path is resolved through `_resolve_claim_token`. A token that
    doesn't resolve is returned UNCHANGED in the second element rather than dropped — dropping
    it would silently pass a role whose claim var was renamed or removed out from under a live
    declaration, which is the same shape as crediting a comment, this time by omission.
    """
    defaults = _role_defaults(role)
    raw: list[str] = []

    tdir = role / "templates"
    for t in sorted(tdir.glob("*.j2")) if tdir.is_dir() else []:
        raw.extend(_pvc_names(t.read_text()))

    for task in _live_tasks(role):
        include = task.get("ansible.builtin.include_role")
        if isinstance(include, dict) and include.get("name") == "k8s/volume-claim":
            claim = (task.get("vars") or {}).get("volume_claim_name")
            if isinstance(claim, str):
                raw.append(claim)

    resolved: set[str] = set()
    unresolved: list[str] = []
    for token in raw:
        name = _resolve_claim_token(token, defaults)
        if name is not None:
            resolved.add(name)
        else:
            unresolved.append(token)
    return resolved, unresolved


_STRATEGY_RECREATE = re.compile(
    r"^\s*strategy:\s*$\n\s*type:\s*Recreate\s*$", re.MULTILINE
)


def _deployment_strategy_is_recreate(role: Path) -> bool:
    """Whether any template `role` renders declares `strategy: / type: Recreate`.

    Comments stripped first so a `# type: Recreate` mentioned in passing (a rationale comment
    on a RollingUpdate role explaining why it ISN'T Recreate, say) can't be credited — the same
    discipline `_strip_comments` exists to enforce everywhere else in this file.
    """
    tdir = role / "templates"
    if not tdir.is_dir():
        return False
    return any(
        _STRATEGY_RECREATE.search(_strip_comments(t.read_text()))
        for t in sorted(tdir.glob("*.j2"))
    )


def _migrating_state(role: Path) -> bool:
    """Whether `role` has the shape volume-snapshot exists for:

    `strategy: Recreate` against at least one rendered RWO PVC claim.

    This is the mechanical definition, read off what the role actually renders — NOT off
    `k8s_autodeploy_reason` text.

    Measured 2026-08-21: this predicate is true for 31 roles, not the thirteen slice 7a task 3
    declared `k8s_autodeploy_snapshot_pvcs` for. `_migrating_state` is broad on purpose — it reads
    `strategy: Recreate` plus a rendered RWO claim off every role, whether or not that role is
    auto-deployable. Before slice 7b task 7 promoted twelve of those thirteen, the 31 roles this
    predicate flagged and the 14 `_auto_deployable` roles did not intersect at all, which is what
    made `test_auto_deployable_migrating_state_roles_declare_snapshot_pvcs` below vacuous. Task 7
    made the two sets overlap on those twelve; the same-day scope decision then re-denied three of
    them (zigbee2mqtt, livesync, qbittorrent — state coupled outside the volume, not a snapshot
    gap), and a later audit re-denied tdarr for the same reason — so the overlap the guard actually
    exercises today is the remaining eight. Every count along the way is non-empty, so the guard
    bites instead of matching an empty loop.

    Almost every PVC `_rendered_pvc_claims` can find in this repo hardcodes `accessModes:
    [ReadWriteOnce]` (both direct templates and k8s/volume-claim's shared one), so a rendered claim
    existing at all is normally sufficient without a separate accessModes read. The one exception:
    `k8s/media-volume`'s own `pvc.yaml.j2` is `ReadWriteMany`. It does not corrupt this predicate
    today — `media-volume` itself renders no Recreate Deployment, so `_migrating_state` never
    reaches that claim — but a future Recreate role sharing that RWX volume would be flagged here as
    if it needed snapshot protection for a migration risk RWX doesn't actually carry the same way
    RWO does.
    """
    return _deployment_strategy_is_recreate(role) and bool(
        _rendered_pvc_claims(role)[0]
    )


# ── state coupled OUTSIDE the volume (2026-08-22 review M2) ─────────────────────────────────
# The exclusion class every other guard in this file misses. The checks above ask whether a role
# protects the claims it OWNS; this one asks whether it mounts a claim it does not own and
# therefore cannot revert.
#
# `_rendered_pvc_claims` reads only what a role CAUSES to exist — a PVC document in its own
# templates, or a `k8s/volume-claim` include. `media-data` is rendered by `k8s/media-volume`, so
# every *arr role mounting it is invisible to that reader. tdarr's promotion was caught by a
# human audit on 2026-08-22, not by a test; sonarr/radarr/bazarr/jellyfin were weighed and kept.
# The gap this closes is the NEXT role added with such a mount, promoted with nobody asked.
#
# The ack key is a list of claim names, not a prose reason — mechanically diffable, and the same
# shape as k8s_autodeploy_snapshot_pvcs. It proves the question was asked, never that the answer
# was right; that is the honest limit of a guard here, and it converts a silent omission into a
# visible one, which is what tdarr needed.
_CLAIM_REF_RE = re.compile(r"^\s*claimName:\s*(?P<token>\S.*?)\s*$", re.MULTILINE)


def _claim_name_refs(role: Path) -> tuple[set[str], list[str]]:
    """Claim names `role`'s own workload templates MOUNT, as `(resolved, unresolved_tokens)`.

    Deliberately distinct from `_rendered_pvc_claims`, which reads what a role creates. A role
    can mount a claim another role renders, and that is exactly the coupling being detected.

    An unresolvable token is returned rather than dropped, and the caller treats it as a
    violation — pihole's `claimName: {{ inst.claim }}` is a loop variable no single-var resolver
    can reach, and a role like that must not slip through as "no refs found". It is denied today,
    so this does not bite; it must stay a violation if one is ever promoted.
    """
    defaults = _role_defaults(role)
    tdir = role / "templates"
    resolved: set[str] = set()
    unresolved: list[str] = []
    for template in sorted(tdir.glob("*.j2")) if tdir.is_dir() else []:
        for match in _CLAIM_REF_RE.finditer(template.read_text()):
            name = _resolve_claim_token(match.group("token"), defaults)
            if name is not None:
                resolved.add(name)
            else:
                unresolved.append(f"{template.name}: {match.group('token')}")
    return resolved, unresolved
