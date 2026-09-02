# ansible/roles/setup/gitops_deploy/files/deploy_k8s.py
"""k8s auto-deploy eligibility, the denylist, and the rollback's revert note.

`split_k8s_auto_deploy` is the gate: diff-shape first (`is_image_only_diff`), identity second
(`declared_denylist`, the pilot scope, the per-tick cap). `declares_snapshot_claims` and
`rollback_volume_revert_note` decide the one revert-status line a rollback alert carries.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Set as AbstractSet
from dataclasses import replace

from deploy_changes import ChangeSet

# A changed line in a unified diff that assigns a container image var, e.g.
#   +speedtest_k8s_image: openspeedtest/latest:v2.0.5
# Anchored on the `_image:` var-name suffix so it matches the same population the
# renovate.json k8s-defaults customManager tracks. Leading whitespace is tolerated; a leading
# `#` is not — a commented-out pin is not an image assignment.
_DIFF_IMAGE_LINE = re.compile(r"^[+-][ \t]*[A-Za-z0-9_]+_image:[ \t]*\S")
# Unified-diff file headers. They begin with -/+ but are metadata, not content, so they must be
# skipped before classifying changed lines.
_DIFF_HEADER = ("--- ", "+++ ")
_K8S_DEFAULTS_PATH = "ansible/roles/k8s/{svc}/defaults/main.yml"


def is_image_only_diff(diff_text: str) -> bool:
    """True when every changed line in `diff_text` assigns an `*_image:` var.

    The diff-shape half of k8s auto-deploy eligibility. Gating on the service NAME alone is not
    enough: `_ACTIVE_K8S` matches the whole role dir, so a name-only gate would also auto-deploy
    configmap / tasks/ / template pushes — none of which carry a Renovate soak behind them.

    Fails closed: a diff with no changed lines (empty, unreadable, header-only) returns False, so
    an unexpected git-output shape defers rather than deploys.
    """
    seen_change = False
    for line in diff_text.splitlines():
        if line.startswith(_DIFF_HEADER):
            continue
        if not line.startswith(("+", "-")):
            continue
        seen_change = True
        if not _DIFF_IMAGE_LINE.match(line):
            return False
    return seen_change


# Roles under roles/k8s/ that deploy no service of their own — they are included by other roles
# and carry no defaults/main.yml, so they declare nothing. Mirrors SHARED_ROLES in
# ansible/filter_plugins/k8s_autodeploy.py; ansible/tests/deploy/test_denylist_parsers_agree.py asserts
# the two stay in step.
SHARED_K8S_ROLES = frozenset({"manifests", "rollout-drain"})

# A top-level `k8s_autodeploy:` assignment, with an optional trailing comment. Anchored at column
# zero deliberately: an indented key of the same name belongs to some other mapping and does not
# declare the role's stance. `[ \t]+` (not `*`) requires a space after the colon, so
# `k8s_autodeploy:true` — which is not a dict key YAML would resolve this way either — doesn't
# match and falls through to "no declaration found", i.e. denied.
_DECLARATION_RE = re.compile(
    r"^k8s_autodeploy:[ \t]+(?P<value>\S+)[ \t]*(?:#.*)?$", re.MULTILINE
)
# The spellings PyYAML resolves to boolean true. Everything else — including an unparseable or
# absent value — counts as denied, so this parser can never widen what may auto-deploy.
_TRUE_VALUES = frozenset(
    {"true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"}
)


def declared_denylist(sources: Mapping[str, str | None]) -> frozenset[str]:
    """Roles denied auto-deploy, read from each role's own `k8s_autodeploy` declaration.

    `sources` maps a role name to the text of its defaults/main.yml, or None when the role has
    no such file. Shared roles are skipped; every other role is denied unless EVERY line that
    looks like a top-level `k8s_autodeploy:` assignment reads as true, and at least one such
    line was found.

    That's unanimity, not "last match wins" — a role is permitted only if every candidate
    assignment agrees. YAML itself is last-key-wins for a genuine duplicate key, so unanimity is
    not what a real YAML parser would do; it's deliberately more conservative. This is a regex,
    not a YAML parser: it can't tell a real top-level `k8s_autodeploy:` key apart from the same
    text sitting inside a multi-line quoted scalar, so on adversarial or malformed input (a
    decoy `true` before the real `false`, a duplicate key) unanimity can only ever add denials
    it would otherwise have missed — never turn a role the last-wins reading would deny into one
    this reader calls permitted. On well-formed, single-declaration input the two rules agree.

    This exists ONLY to detect that /etc/gitops-deploy/config.env has gone stale against
    origin — it never decides eligibility. So it is deliberately biased: anything unclear reads
    as denied, which can make the comparison mismatch and disarm auto-deploy, but can never make
    a denied role look permitted. The authoritative derivation is the Ansible filter, which
    parses real YAML; ansible/tests/deploy/test_denylist_parsers_agree.py pins the two together.
    """
    denied = set()
    for role, text in sources.items():
        if role in SHARED_K8S_ROLES:
            continue
        if not text:
            denied.add(role)
            continue
        values = [m.group("value") for m in _DECLARATION_RE.finditer(text)]
        if not values or any(v not in _TRUE_VALUES for v in values):
            denied.add(role)
    return frozenset(denied)


# A top-level, single-line `k8s_autodeploy_snapshot_pvcs: [...]` list literal — every role that
# declares one today writes it this way (e.g. `[bazarr-config]`, `[tdarr-configs, tdarr-server]`).
# Anchored at column zero for the same reason as _DECLARATION_RE: an indented key belongs to some
# other mapping.
_SNAPSHOT_CLAIM_RE = re.compile(
    r"^k8s_autodeploy_snapshot_pvcs:[ \t]*\[(?P<items>[^\]]*)\]", re.MULTILINE
)


def declares_snapshot_claims(text: str | None) -> bool:
    """Whether a role's defaults/main.yml declares at least one PVC for k8s/volume-revert.

    Two consumers, and they want opposite things from an unparseable input:

    * wording gitops-deploy's rollback alert (which services in the batch a volume revert can
      affect) — False under-claims, which is safe for a line that must never overclaim;
    * gating `split_k8s_auto_deploy`'s per-tick cap on claim-declaring services (2026-08-22
      review H2) — False reads as claim-free and lets the service batch, which is the overrun
      the cap exists to prevent.

    Absent, empty (`[]`), multi-line, or unparseable all read as False. What keeps the second
    consumer honest is not this function but
    test_deploy_k8s_declarations.py::test_declares_snapshot_claims_agrees_with_yaml_for_every_k8s_role,
    which pins this regex against `yaml.safe_load` for every k8s role — so a reformat to block
    style fails CI instead of silently widening the cap. Neither consumer is the authority on
    the revert itself: roles/k8s/manifests decides that from real YAML.
    """
    if not text:
        return False
    m = _SNAPSHOT_CLAIM_RE.search(text)
    return bool(m and m.group("items").strip())


def rollback_volume_revert_note(
    services: AbstractSet[str],
    reverting: AbstractSet[str],
    rollback_failed: str | None,
) -> str:
    """One line for gitops-deploy's rollback-failure Discord alert.

    States plainly whether the volume revert to the pre-deploy snapshot actually ran and for
    which services — "was my data rolled back too" is the first question during an incident.

    `rollback_failed`, when given, is the rollback redeploy's own exception message: the
    redeploy raised before Ansible could reach the revert task (or during it), so the revert may
    never have completed, and the note must say that plainly rather than claim it ran.

    `reverting` is the subset of `services` whose role defaults declare
    `k8s_autodeploy_snapshot_pvcs` — volume-revert is a no-op for the rest even when the redeploy
    itself succeeds, so naming only `services` would overclaim which ones actually changed.
    """
    if rollback_failed is not None:
        return (
            f"**The rollback redeploy itself failed** (`{rollback_failed}`) — the volume revert "
            f"task may never have completed. Check whether `{', '.join(sorted(services))}` is "
            f"sitting at zero replicas with a volume attached in Longhorn maintenance mode.\n"
        )
    if not reverting:
        return (
            f"No service in `{', '.join(sorted(services))}` declares "
            f"`k8s_autodeploy_snapshot_pvcs` — no volume revert applies.\n"
        )
    no_claim = sorted(services - reverting)
    line = f"Volume revert to the pre-deploy snapshot targets `{', '.join(sorted(reverting))}`"
    if no_claim:
        line += (
            f" (`{', '.join(no_claim)}` declares no `k8s_autodeploy_snapshot_pvcs` and is "
            f"unaffected)"
        )
    return line + ".\n"


def k8s_role_paths(listing: str) -> dict[str, str | None]:
    """Map each k8s role to its defaults/main.yml path in a `git ls-tree -r --name-only` listing.

    A role with no defaults/main.yml at that ref maps to None, which declared_denylist() reads
    as denied. Paths look like `ansible/roles/k8s/<role>/<rest...>`, so the role name is the
    FOURTH segment — this indexing shipped an off-by-one once already.

    Pure string parsing, callable on its own so it's unit-testable without git; the I/O caller
    is gitops_deploy.k8s_declarations_at.
    """
    roles: dict[str, str | None] = {}
    for path in listing.splitlines():
        # "ansible/roles/k8s/<role>/<rest...>" — a real role always has a file at least one
        # directory deep, so anything shallower (a stray file directly under roles/k8s/) is
        # not a role and must not be recorded as one.
        parts = path.split("/")
        if len(parts) < 5:
            continue
        role = parts[3]
        if parts[4] == "defaults" and path.endswith("defaults/main.yml"):
            roles[role] = path
        else:
            # Still record the role so one with no defaults/ at all is visible as None. A
            # setdefault here never clobbers an already-found defaults/main.yml path, and
            # doesn't need to run before it either — the listing's order doesn't matter.
            roles.setdefault(role, None)
    return roles


def split_k8s_auto_deploy(
    cs: ChangeSet,
    paths: list[str],
    *,
    denylist: frozenset[str] | set[str],
    pilot: frozenset[str] | set[str],
    enabled: bool,
    image_only: Callable[[str], bool],
    max_per_tick: int = 0,
    declares_claims: Callable[[str], bool] | None = None,
    max_claim_services_per_tick: int = 0,
) -> ChangeSet:
    """Promote image-bump-only k8s changes from `cs.k8s` into `cs.k8s_deploy`.

    A service qualifies only when ALL of:
      * the feature is enabled;
      * it is not denylisted (platform / observability / migrating-state / dependency-edge /
        stateful / nothing-to-gate / probe-less / games — each entry's reason is in the role
        defaults and the design doc's denylist table);
      * `pilot` is empty, or the service is named there (the slice-1 pilot scope);
      * every path this push changed under that role dir is exactly its defaults/main.yml;
      * `image_only(svc)` — that file's diff touches only `*_image:` lines.

    The path check and the diff check are BOTH required: the path check alone would admit a push
    editing defaults/main.yml's non-image vars, and the diff check alone would admit a push that
    also edits tasks/.

    Two caps then bound what one tick takes on: `max_per_tick` on the batch as a whole, and
    `max_claim_services_per_tick` on services declaring `k8s_autodeploy_snapshot_pvcs` — whose
    snapshot+revert cost is additive across a batch while the rollback budget is derived for one.
    Surplus in either direction stays in `cs.k8s` and defer-and-alerts.

    Fail-closed by construction — anything not promoted stays in `cs.k8s`, which defer-and-alerts
    exactly as it does today.
    """
    if not enabled:
        return cs
    if cs.services:
        # A tick carrying Docker services too. The caller's k8s branch returns before reaching
        # the Docker deploy + health gate, so promoting here would silently skip them. No host
        # is mixed today (daniel-box is all-k8s and is the only has_gitops host), so defer the
        # k8s half rather than grow a two-plane tick ordering this deployer doesn't model.
        return cs
    promoted: set[str] = set()
    for svc in cs.k8s:
        if svc in denylist:
            continue
        if pilot and svc not in pilot:
            continue
        role_prefix = f"ansible/roles/k8s/{svc}/"
        changed_here = [p for p in paths if p.startswith(role_prefix)]
        if changed_here != [_K8S_DEFAULTS_PATH.format(svc=svc)]:
            continue
        if not image_only(svc):
            continue
        promoted.add(svc)
    if not promoted:
        return cs
    # Cap what one tick takes on. deploy_k8s joins every promoted service into a SINGLE
    # ansible-playbook invocation under one shared K8S_DEPLOY_TIMEOUT_S, and on timeout the
    # failure path is `git reset --hard local` across the whole merged range — so an overlong
    # batch discards the good bumps alongside the bad one. renovate.json states the intent
    # ("keeps each auto-deploy tick to a single service, which is what the deploy timeout is
    # sized for") but nothing enforced it: a tick diffs local..origin, spanning every commit
    # since the last one, and per-service k8s PRs share one daily window with platformAutomerge.
    #
    # The surplus stays in cs.k8s, which defer-and-alerts — the same fail-closed path as any
    # unpromotable change. It is NOT picked up on a later tick, and it is worth being exact
    # about that: the ff-merge runs before deploy_k8s, so once the tick succeeds local == origin
    # and next_action() returns "noop" from then on. renovate.json states this correctly for the
    # same mechanism. What actually carries the surplus is the Discord message alert_deferred()
    # posts, which names the services and the tags to deploy them by hand — so the surplus is
    # operator-visible, but only once, and unlike hold_sha/diverged_sha/behind_since it leaves no
    # state behind for a monitor to notice.
    #
    # Do NOT "fix" this by deferring the ff-merge: that strands the tree behind pods already
    # running the new images.
    #
    # SECOND cap, on claim-declaring services specifically (2026-08-22 review H2). The rollback
    # budget K8S_ROLLBACK_TIMEOUT_S is derived for the worst SINGLE promoted service that
    # declares k8s_autodeploy_snapshot_pvcs — but deploy_k8s joins the whole batch into one
    # playbook run, and each such service pays its own snapshot + revert phase serially inside
    # it (only the rollout WAIT is deduped, by k8s/rollout-drain). Measured from role sources:
    # one radarr/sonarr-shaped service is ~1200s against a 1320s budget, two co-batched ~1680s,
    # three ~2160s. Past the budget `run()`'s killpg fires MID-REVERT — after volume-revert has
    # scaled the workload to zero replicas and attached the volume with disableFrontend: true —
    # stranding a service at zero replicas with its volume in Longhorn maintenance mode.
    #
    # Not hypothetical: slice 7b co-promoted five lscr.io/linuxserver/* siblings (bazarr,
    # jellyfin, prowlarr, radarr, sonarr) that share one Renovate automerge window with no
    # stagger, so a 2-3 sibling tick is ordinary.
    #
    # Capping claim-declaring services rather than lowering max_per_tick is deliberate: claim-free
    # services cost nothing on the revert path and should still batch. Per-service ROLLBACK
    # invocation — the alternative the role CLAUDE.md proposes — does NOT fit: 3 x 1200 + 900
    # forward + 180 flock exceeds the unit's 2700s TimeoutStartSec.
    #
    # `declares_claims` is injected (like `image_only`) so this stays a pure function; the caller
    # reads each role's defaults at the PINNED origin SHA. It resolves False for a declaration the
    # regex cannot parse, which for ALERT WORDING was the safe direction and for GATING is not —
    # a multi-line declaration would read as claim-free and batch. What holds that closed is
    # test_deploy_k8s_declarations.py::test_declares_snapshot_claims_agrees_with_yaml_for_every_k8s_role,
    # which pins the regex verdict against yaml.safe_load for every k8s role, so a reformat fails
    # CI rather than silently widening this cap.
    if declares_claims is not None and max_claim_services_per_tick > 0:
        ordered = sorted(promoted)
        claim_services = [svc for svc in ordered if declares_claims(svc)]
        claim_free = [svc for svc in ordered if svc not in set(claim_services)]
        kept = claim_services[:max_claim_services_per_tick]
        room = (
            len(claim_free) if max_per_tick <= 0 else max(0, max_per_tick - len(kept))
        )
        promoted = set(kept + claim_free[:room])
    elif max_per_tick > 0 and len(promoted) > max_per_tick:
        promoted = set(sorted(promoted)[:max_per_tick])
    # Both branches above already respect max_per_tick (the first via `room`), so there is no
    # third clamp here — one would be able to drop the claim-declaring service the first branch
    # deliberately kept.
    if not promoted:
        # Every promotable service was a surplus claim-declaring one. Returning `cs` unchanged
        # leaves them all in cs.k8s, which defer-and-alerts — the same fail-closed path as any
        # unpromotable change.
        return cs
    return replace(cs, k8s=cs.k8s - promoted, k8s_deploy=cs.k8s_deploy | promoted)
