#!/usr/bin/env python3
"""Whether a deploy-plane tick applies a narrowed `--tags` list or today's whole play.

`handle_broad` used to run `ansible/deploy.yml` unscoped for any change under
`ansible/templates/` or `ansible/inventory/`. That is ~20 minutes under the tree lock across
every role, it was 47% of all lock-busy time from 2026-09-04, and twice it failed on a gate
belonging to a service the change never touched and held the fleet.

The derivation lives in `scripts/deploy_tools/narrow_broad.py` and is reached as a
SUBPROCESS, not imported: it parses YAML and this unit runs under `uv run --no-project`,
which cannot import yaml. `narrow_deploy_plane` is that boundary and is a `DeployTools`
field, so a test replaces it rather than a module attribute.

Reach `deploy_io` and `deploy_alerts` qualified, never by from-import.
"""

import subprocess
from typing import Callable, NamedTuple

from deploy_config import log

# What `record_broad_applied` records in the tag slot when a deploy-plane range moves no
# rendered output at all — a comment-only inventory edit, a variable nothing reads, a macro
# nothing imports. The fast-forward IS the whole apply there, so the marker has to say that
# an apply happened without naming tags that were never passed to anything.
NARROWED_TO_NOTHING = "narrowed-to-nothing"

# How long the derivation gets. It is a handful of `git grep`s over a 54-role tree, so a run
# that is still going at two minutes has wedged rather than slowed; the tick then takes the
# full play, which is what it did before this existed.
NARROW_TIMEOUT_S = 120.0

NARROW_SCRIPT = "scripts/deploy_tools/deploy_tags.py"

# How long the setup-role narrowing gets, and the script that does it. Cheaper than the
# deploy-plane derivation — one `git ls-tree` plus a `git show` per file of ONE role — so a run
# still going at thirty seconds has wedged.
NARROW_SETUP_TIMEOUT_S = 30.0
NARROW_SETUP_SCRIPT = "scripts/deploy_tools/narrow_setup.py"


class BroadPlan(NamedTuple):
    """What one broad tick applies.

    Attributes:
        playbook: the playbook to run.
        tags: its `--tags` value, empty for the whole playbook.
        apply: False when there is nothing to run, and the ff-merge is the whole apply.
    """

    playbook: str
    tags: list[str]
    apply: bool


def narrow_deploy_plane(
    repo: str, old: str, new: str, timeout: float
) -> tuple[int, str]:
    """Ask `deploy_tags.py narrow` which tags the range `old..new` reaches.

    Args:
        repo: the checkout to run in, which is also the tree the derivation reads.
        old: the commit the checkout is on.
        new: the commit it is about to fast-forward to.
        timeout: seconds before the child is killed.

    Returns:
        (exit code, stdout). Exit 0 with a comma-joined tag list narrows; exit 0 with empty
        stdout means the range reaches no rendered output; anything else is a refusal.

    Raises:
        subprocess.TimeoutExpired: the child outlived `timeout`.
        OSError: the child could not be started.
        Exception: anything else the subprocess layer raises reaches the caller unchanged.
            `text=True` decodes strictly, so undecodable output is a UnicodeDecodeError,
            which is a ValueError and neither of the two above. That is why `_deploy_plane`
            catches `Exception` rather than a tuple.

    `uv run --frozen` for the same reason `deploy_io.deploy` uses it: the repo's pinned env,
    never mutating uv.lock on the host. Every stderr line is logged, so the journal carries
    the derivation the tick acted on — `narrow: <key> -> <tags> via <path>`.
    """
    r = subprocess.run(
        ["uv", "run", "--frozen", "python", NARROW_SCRIPT, "narrow", old, new],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    for line in r.stderr.splitlines():
        if line.strip():
            log(line.strip())
    return r.returncode, r.stdout.strip()


def narrow_setup_role(
    repo: str, role: str, role_tag: str, old: str, new: str, timeout: float
) -> tuple[int, str]:
    """Ask `narrow_setup.py` which of `role`'s own tags the range `old..new` actually needs.

    Args:
        repo: the checkout to run in, which is also the tree the derivation reads.
        role: the role directory under `ansible/roles/setup/`.
        role_tag: the `--tags` value selecting the whole role, which the answer must not be.
        old: the commit the checkout was on.
        new: the commit carrying the change.
        timeout: seconds before the child is killed.

    Returns:
        (exit code, stdout). Exit 0 with a comma-joined tag list narrows; anything else is a
        refusal, and the caller records no narrowing so every surface prints the role tag.

    A subprocess for the reason `narrow_deploy_plane` is one: the derivation parses YAML and
    this unit runs under `uv run --no-project`. Every stderr line is logged, so the journal
    carries the per-path derivation the marker was written from.
    """
    r = subprocess.run(
        [
            "uv",
            "run",
            "--frozen",
            "python",
            NARROW_SETUP_SCRIPT,
            role,
            old,
            new,
            "--role-tag",
            role_tag,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    for line in r.stderr.splitlines():
        if line.strip():
            log(line.strip())
    return r.returncode, r.stdout.strip()


def plan(
    narrow: Callable[[str, str, str, float], tuple[int, str]],
    config,
    target,
    setup_tags: set[str],
    deploy_plane: bool,
) -> list[BroadPlan]:
    """What this broad tick applies, in order: the setup plane's tags, then the deploy plane's.

    Args:
        narrow: `narrow_deploy_plane`, or a test's stand-in for it.
        config: the tick's `Config`, for the checkout path.
        target: the tick's `TickTarget`, for the two commits bounding the range.
        setup_tags: `setup_tags_for(paths)`, non-empty for a setup-plane change.
        deploy_plane: `cs.broad_deploy` — the range moved a deploy-plane path.

    A range that carries both planes gets both plans. Until 2026-09-18 this was an if/else
    and the setup arm won: a `roles/setup/` edit landing beside a `host_vars/` edit applied
    `initial_setup.yml` and dropped the deploy plane without a journal line (#2046) — two
    Pi retirements that day left `Release Staleness Drift` DOWN over 56 records the full
    `deploy.yml` a removed `containers_list` entry refused into (until #2044) was meant to
    re-stamp. The setup arm is unchanged: its tags were already derived, by `setup_tags_for`.
    """
    plans = []
    if setup_tags:
        plans.append(BroadPlan("ansible/initial_setup.yml", sorted(setup_tags), True))
    if deploy_plane:
        plans.append(_deploy_plane(narrow, config, target))
    return plans


def _deploy_plane(narrow, config, target) -> BroadPlan:
    """The deploy plane: a narrowed `--tags`, nothing at all, or the whole play.

    Whichever it is, `config.k8s_autodeploy_denylist` does not subtract from it — see
    `denylisted_in` and the DECIDED marker on it.
    """
    playbook = "ansible/deploy.yml"
    try:
        rc, out = narrow(config.repo, target.local, target.origin, NARROW_TIMEOUT_S)
    except Exception as exc:
        return _full_run(playbook, f"{type(exc).__name__}: {exc}")
    # DECIDED: a full run on any doubt. Every way the derivation can be unsure — a variable
    # the play itself reads, a tag list covering most of
    # the fleet, a crash here — lands on this branch and runs what the tick ran before.
    # `except Exception` is deliberate and the narrowest correct width: the call decodes a
    # subprocess's output, so it can raise UnicodeDecodeError as well as SubprocessError,
    # and an escape parks every landing behind this tick — `plan` runs BEFORE the ff-merge.
    # A missed consumer is a service left silently stale until something unrelated
    # redeploys it, and nothing reports that; a full run is only slow. The rules and what
    # each one refuses are in scripts/deploy_tools/narrow_broad.py.
    if rc != 0:
        return _full_run(playbook, f"exit {rc}")
    tags = [t for t in out.split(",") if t]
    if not tags:
        # A tag list nothing would match is NOT the same as no tags: `--tags <nothing>`
        # still runs every `tags: always` task in the play, and an empty `--tags` value runs
        # the whole playbook. So this applies neither, and says so in the marker instead.
        log("narrow: the range moves no rendered output — merged, applying nothing")
        return BroadPlan(playbook, [NARROWED_TO_NOTHING], False)
    log(f"narrow: applying {playbook} --tags {','.join(tags)}")
    denied = denylisted_in(tags, config.k8s_autodeploy_denylist)
    if denied:
        log(
            f"narrow: {','.join(denied)} are denylisted for k8s auto-deploy and are applied "
            "here regardless — the broad plane runs the plain playbook forward-only, with "
            "no snapshot, staging gate or rollback for the denylist to withhold"
        )
    return BroadPlan(playbook, tags, True)


# DECIDED: the broad plane ignores `K8S_AUTODEPLOY_DENYLIST` (issue #1962). The denylist gates
# PROMOTION into the k8s auto-deploy machinery — `split_k8s_auto_deploy` — whose pre-apply
# snapshot, staging gate and automatic rollback are what a role declares `k8s_autodeploy:
# false` to stay out of: a probe-less workload the rollout gate cannot see, migrating state a
# revert would corrupt, a platform role whose rollback needs the access it just broke. The
# broad plane has none of that machinery. It runs the plain playbook forward-only and holds
# the SHA on failure, which is what an operator's `deploy.sh --tags <role>` does for the same
# role, and it has applied every denied role that way on every unscoped run since 2026-08-29.
# Three things made a filter the wrong shape. It could only gate the NARROWED path: a refused
# range still runs the whole play, over all forty denied roles, so the filter would gate the
# derivation that is certain and leave the one that is not wide open. Forty of the fifty-four
# k8s roles are denied, so a filtered narrowed range would mostly apply nothing and hand a
# tag list to a human — for a change a human authored and merged, with `land.sh` already
# waiting on this tick to apply it. The third reason, that a dropped tag would be a service
# left silently stale with nothing to name it, no longer holds on its own: since #1993
# `Release Staleness Drift` asks `narrow_broad` the same per-path question this plan asks,
# from each service's release record to origin/master, so a denied role whose render reads a
# merged inventory key or macro reads STALE until it is re-stamped (`_deploy_plane_stale` in
# scripts/diagnostics/probe_lib/releases.py). That makes a filter viable, not wanted: the
# first two reasons stand, and `manual_plane` is still rendered as a setup role by every one
# of its five readers, so a deferral here would still have no k8s-shaped marker. What the
# denylist still governs on this plane is who MERGES: renovate.json's `manual —
# k8s_autodeploy: false` rule names `group_vars/all.yml` beside a denied role's own defaults
# (#1936), so no pin a denied role reads through a shared key merges unattended.
# `denylisted_in` exists so the journal line above can name which of a narrowed apply's tags
# were denied ones; the `broad_applied` marker carries the same tag list durably, and the
# denied subset of it is that list intersected with the denylist.
def denylisted_in(tags: list[str], denylist: frozenset[str] | set[str]) -> list[str]:
    """The tags in a narrowed list that `K8S_AUTODEPLOY_DENYLIST` names, in the list's order.

    Named in the journal and nowhere else; nothing drops them. The DECIDED above says why.
    """
    return [t for t in tags if t in denylist]


def _full_run(playbook: str, reason: str) -> BroadPlan:
    """The fallback, logged every tick it is taken so the journal says why the play was whole."""
    log(f"narrow: cannot narrow ({reason}) — full {playbook}")
    return BroadPlan(playbook, [], True)
