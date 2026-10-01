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

import json
import subprocess
import time
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

# The playbook the setup arm runs, and the one `narrow_setup.py` derives a tag's reachability
# against. A literal here rather than `deploy_changes.setup_role_playbook`, which is the
# authority: `test_gitops_deploy_imports` holds this module to `deploy_config` alone, and
# `ansible/tests/deploy/test_setup_role_playbooks_agree.py` is what keeps the string honest.
SETUP_PLAYBOOK = "ansible/initial_setup.yml"

# How long the setup-role narrowing gets, and the script that does it. Cheaper than the
# deploy-plane derivation — one `git ls-tree` plus a `git show` per file of ONE role — so a run
# still going at thirty seconds has wedged.
NARROW_SETUP_TIMEOUT_S = 30.0
NARROW_SETUP_SCRIPT = "scripts/deploy_tools/narrow_setup.py"

# What the whole setup-narrowing LOOP gets, across every role one range touches. The per-role
# budget bounds one child; nothing bounds how many roles a range carries, and `plan` runs
# BEFORE the ff-merge and inside the unit's TimeoutStartSec, which the phase budgets in
# `defaults/main.yml` already fill. Three roles at the per-role budget is the shape measured
# ranges reach; past that the remaining roles take their whole-role tag, which is what they
# did before #3120.
NARROW_SETUP_TOTAL_BUDGET_S = 3 * NARROW_SETUP_TIMEOUT_S

# How long the shared-role caller derivation gets, and the script that does it. One walk of
# the k8s role tree's tasks, so thirty seconds is the wedged case, as for the setup narrowing.
SHARED_CALLERS_TIMEOUT_S = 30.0
SHARED_CALLERS_SCRIPT = "scripts/deploy_tools/shared_role_callers.py"
# Which own roles a render digest can prove (#3110); one YAML walk per role, same budget.
DIGEST_PROVABLE_SCRIPT = "scripts/deploy_tools/digest_provable.py"


class BroadPlan(NamedTuple):
    """What one broad tick applies.

    Attributes:
        playbook: the playbook to run.
        tags: its `--tags` value, empty for the whole playbook.
        apply: False when there is nothing to run, and the ff-merge is the whole apply.
        hold_tags: what a FAILED apply holds, when that is wider than what it ran. None
            means the two are the same, which is every plan but the narrowed setup plane.
    """

    playbook: str
    tags: list[str]
    apply: bool
    hold_tags: list[str] | None = None

    # DECIDED: the narrowed setup plane holds the ROLE tags it narrowed FROM, not the block
    # tags it ran. `broad_hold_cleared_by` compares tag STRINGS, so a hold naming
    # `gitops-config` is not covered by a later apply of `--tags gitops_deploy`, even though
    # that run applies that block and every other one in the role. Holding the role tag keeps
    # the clear-side behaviour this deployer had before #3120, where a later apply of the same
    # role always clears the hold: the whole-role fallback fires on most ranges, so the
    # alternative is a hold that survives the apply that fixed it, parking every session's
    # landing. It over-claims, since the failure may have been one block — and over-claiming a
    # hold is the safe direction, because issue #878 is the false clear, not the sticky one.
    # The precise per-block version needs the role in the marker: issue #3138.
    @property
    def held(self) -> list[str]:
        """The tags a failed apply records in `hold_plane` and the Discord alert quotes."""
        return self.tags if self.hold_tags is None else self.hold_tags


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


def narrow_setup_argv(
    role: str, role_tag: str, playbook: str, old: str, new: str
) -> list[str]:
    """The command `narrow_setup_role` runs.

    Split out so a test can hand its arguments to `narrow_setup.main` itself. The fakes
    replace the subprocess, and with it the only place a mismatched flag would otherwise show.
    """
    return [
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
        "--playbook",
        playbook,
    ]


def narrow_setup_role(
    repo: str,
    role: str,
    role_tag: str,
    playbook: str,
    old: str,
    new: str,
    timeout: float,
) -> tuple[int, str]:
    """Ask `narrow_setup.py` which of `role`'s own tags the range `old..new` actually needs.

    Args:
        repo: the checkout to run in, which is also the tree the derivation reads.
        role: the role directory under `ansible/roles/setup/`.
        role_tag: the `--tags` value selecting the whole role, which the answer must not be.
        playbook: the playbook the remediation prints for `role`. The derivation refuses a
            tag that playbook's entry for the role cannot reach.
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
        narrow_setup_argv(role, role_tag, playbook, old, new),
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


def shared_callers_argv(roles) -> list[str]:
    """The command `shared_role_callers` runs, split out for the reason `narrow_setup_argv` is."""
    return ["uv", "run", "--frozen", "python", SHARED_CALLERS_SCRIPT, *sorted(roles)]


def shared_role_callers(repo: str, roles) -> dict[str, set[str]]:
    """Ask `shared_role_callers.py` which tags' deploys run each shared role.

    A subprocess for the reason `narrow_deploy_plane` is one. The one reader,
    `deploy_defer.discharge_k8s_unapplied`, keeps every line on any exception this raises.

    Raises:
        subprocess.CalledProcessError: the child exited non-zero.
        subprocess.TimeoutExpired: the child outlived `SHARED_CALLERS_TIMEOUT_S`.
        ValueError: its stdout was not the JSON object it prints.
    """
    out = _role_json(repo, shared_callers_argv(roles))
    return {role: set(tags) for role, tags in out.items()}


def digest_provable(repo: str, roles) -> set[str]:
    """Ask `digest_provable.py` which of `roles` act only through their render digest (#3110).

    A subprocess for the reason `shared_role_callers` is one, raising what it raises. The one
    reader, `deploy_defer.discharge_k8s_unapplied`, treats any exception as "none are".
    """
    out = _role_json(repo, digest_provable_argv(roles))
    return {role for role, ok in out.items() if ok is True}


def digest_provable_argv(roles) -> list[str]:
    """The command `digest_provable` runs, split out for the reason `narrow_setup_argv` is."""
    return ["uv", "run", "--frozen", "python", DIGEST_PROVABLE_SCRIPT, *sorted(roles)]


def _role_json(repo: str, argv: list[str]) -> dict:
    """Run one of the YAML-reading role scripts in `repo` and decode the object it prints."""
    r = subprocess.run(
        argv,
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=SHARED_CALLERS_TIMEOUT_S,
        check=False,
    )
    for line in r.stderr.splitlines():
        if line.strip():
            log(line.strip())
    r.check_returncode()
    out = json.loads(r.stdout)
    if not isinstance(out, dict):
        raise ValueError(f"{argv[4]} printed {type(out).__name__}, not an object")
    return out


def narrowed_setup_tags(
    narrow_setup: Callable[..., tuple[int, str]],
    config,
    target,
    setup_tags: set[str],
    setup_roles: dict[str, str],
    now: Callable[[], float] = time.monotonic,
) -> list[str]:
    """`setup_tags`, with each role tag replaced by the block tags its own diff reaches (#3120).

    Args:
        narrow_setup: `narrow_setup_role`, or a test's stand-in for it.
        config: the tick's `Config`, for the checkout path.
        target: the tick's `TickTarget`, for the two commits bounding the range.
        setup_tags: `setup_tags_for(paths)` — the whole-role tags this tick would apply.
        setup_roles: role tag -> role directory, for the roles `SETUP_PLAYBOOK` applies.
        now: the monotonic clock the shared budget below is measured on. A parameter so a
            test can spend the budget without sleeping.

    Returns:
        The sorted union of what each role needs. `--tags initial_setup` selects about 440
        tasks; since #3116 every one of them carries a tag narrower than `crons`, so the tags
        a changed file maps to can be derived — and a run that applies only those is a
        smaller blast radius for a bad template and a shorter setup-plane tick.

    PER ROLE, and never all-or-nothing. One role narrowing while another refuses applies the
    first role's block tags beside the second's whole-role tag, which is the only shape that
    narrows a mixed range at all.

    The loop shares ONE wall-clock budget, `NARROW_SETUP_TOTAL_BUDGET_S`. Once it is spent
    every remaining role takes its whole-role tag, so a range touching many setup roles
    cannot spend the per-role budget once per role ahead of the ff-merge.

    A tag no role in `setup_roles` claims passes through untouched. `collections` is
    `ansible/requirements.yml`'s, mapped to no role directory, so there is nothing to derive
    it from — and dropping it would leave the collections uninstalled with nothing said.
    """
    out: set[str] = set()
    deadline = now() + NARROW_SETUP_TOTAL_BUDGET_S
    for tag in sorted(setup_tags):
        role = setup_roles.get(tag)
        if role is None:
            out.add(tag)
            continue
        left = deadline - now()
        if left <= 0:
            out |= _whole_role(role, tag, "this tick's narrowing budget is spent")
            continue
        out |= _one_setup_role(
            narrow_setup, config, target, role, tag, min(NARROW_SETUP_TIMEOUT_S, left)
        )
    return sorted(out)


def _one_setup_role(
    narrow_setup, config, target, role: str, role_tag: str, timeout: float
) -> set[str]:
    """One role's narrow tags, or `{role_tag}` with the refusal logged.

    DECIDED: the whole-role tag on any doubt, the way `_deploy_plane` takes the full play.
    `narrow_setup.role_tags` already refuses on every ambiguity it can name — an untagged task
    file, a tag a second role declares, a derivation landing back on the role tag — and this
    adds the ones it cannot: a non-zero exit, empty output, and anything the subprocess layer
    raises. `except Exception` is deliberate and the same width `_deploy_plane` uses: the call
    decodes a child's output, so UnicodeDecodeError is as reachable as SubprocessError, and
    `plan` runs BEFORE the ff-merge, where an escape parks every session's landing. A tag list
    that is too wide is only slow; one that is too narrow leaves the change unapplied behind a
    run that exited 0.
    """
    try:
        rc, out = narrow_setup(
            config.repo,
            role,
            role_tag,
            SETUP_PLAYBOOK,
            target.local,
            target.origin,
            timeout,
        )
    except Exception as exc:
        return _whole_role(role, role_tag, f"{type(exc).__name__}: {exc}")
    if rc != 0:
        return _whole_role(role, role_tag, f"exit {rc}")
    tags = {tag for tag in out.split(",") if tag}
    if not tags:
        # Exit 0 with nothing to apply is not a refusal the derivation can make — every
        # branch of `role_tags` either returns a non-empty set or raises. Treated as doubt
        # rather than as "apply nothing": an empty `--tags` value runs the WHOLE playbook.
        return _whole_role(role, role_tag, "it exited 0 and printed no tags")
    log(f"narrow-setup: {role} needs {','.join(sorted(tags))}, not {role_tag}")
    return tags


def _whole_role(role: str, role_tag: str, reason: str) -> set[str]:
    """The fallback, logged every tick it is taken, as `_full_run` is for the deploy plane."""
    log(f"narrow-setup: cannot narrow {role} ({reason}) — applying --tags {role_tag}")
    return {role_tag}


def plan(
    narrow: Callable[[str, str, str, float], tuple[int, str]],
    config,
    target,
    setup_tags: set[str],
    deploy_plane: bool,
    narrow_setup: Callable[..., tuple[int, str]],
    setup_roles: dict[str, str],
    digest_diff: Callable[[str], dict[str, list[str]]] | None = None,
) -> list[BroadPlan]:
    """What this broad tick applies, in order: the setup plane's tags, then the deploy plane's.

    Args:
        narrow: `narrow_deploy_plane`, or a test's stand-in for it.
        config: the tick's `Config`, for the checkout path.
        target: the tick's `TickTarget`, for the two commits bounding the range.
        setup_tags: `setup_tags_for(paths)`, non-empty for a setup-plane change.
        deploy_plane: `cs.broad_deploy` — the range moved a deploy-plane path.
        narrow_setup: `narrow_setup_role`, or a test's stand-in, for the setup arm.
        setup_roles: role tag -> role directory, for the roles `SETUP_PLAYBOOK` applies.
        digest_diff: `deploy_release.digest_diff`, read for the shadow log alone. None skips
            the log.

    A range that carries both planes gets both plans. Until 2026-09-18 this was an if/else
    and the setup arm won: a `roles/setup/` edit landing beside a `host_vars/` edit applied
    `initial_setup.yml` and dropped the deploy plane without a journal line (#2046) — two
    Pi retirements that day left `Release Staleness Drift` DOWN over 56 records the full
    `deploy.yml` a removed `containers_list` entry refused into (until #2044) was meant to
    re-stamp.

    Both arms narrow since #3120. `setup_tags_for` derives WHICH role tag each path needs, and
    `narrowed_setup_tags` then narrows each of those to the role's own block tags.
    """
    plans = []
    if setup_tags:
        tags = narrowed_setup_tags(
            narrow_setup, config, target, setup_tags, setup_roles
        )
        plans.append(BroadPlan(SETUP_PLAYBOOK, tags, True, sorted(setup_tags)))
    if deploy_plane:
        deploy = _deploy_plane(narrow, config, target)
        if digest_diff is not None:
            log_digest_shadow(digest_diff, target.origin, deploy)
        plans.append(deploy)
    return plans


# DECIDED: shadow mode first (#3045). The render-digest diff is LOGGED beside what the
# narrowing chose and decides nothing yet. A render record exists only for a service
# `render_targets.py` lists — a daniel-box k8s entry whose role includes `k8s/manifests` — and
# the hourly producer writes it at the newest green commit, so at a tick that has just
# fetched a merge the commit being applied usually has no render at all. A week of these
# lines is what measures how often the diff could answer; a service with no usable record
# would keep this function's answer, and the full run stays the fallback either way.
def log_digest_shadow(digest_diff, origin: str, deploy: BroadPlan) -> None:
    """Log the tags a render-digest diff at `origin` would apply, beside `deploy`'s answer.

    Never raises: it runs before the ff-merge, where an escaped exception parks every landing.
    """
    try:
        verdicts = digest_diff(origin)
    except Exception as exc:
        log(f"narrow shadow: no digest diff ({type(exc).__name__}: {exc})")
        return
    drifted = verdicts.get("drifted", [])
    unknown = ", ".join(
        f"{key.split(': ', 1)[1]} {len(names)}"
        for key, names in sorted(verdicts.items())
        if key.startswith("unknown: ")
    )
    if not deploy.apply:
        chose = "nothing"
    elif deploy.tags:
        chose = ",".join(deploy.tags)
    else:
        chose = "the full play"
    log(
        f"narrow shadow: render digest at {origin[:8]} would apply "
        f"{','.join(drifted) or 'nothing'} (current {len(verdicts.get('current', []))}; "
        f"unknown: {unknown or 'none'}); the narrowing chose {chose}"
    )


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
            "no snapshot or rollback for the denylist to withhold"
        )
    return BroadPlan(playbook, tags, True)


# DECIDED: the broad plane ignores `K8S_AUTODEPLOY_DENYLIST` (issue #1962). The denylist gates
# PROMOTION into the k8s auto-deploy machinery — `split_k8s_auto_deploy` — whose pre-apply
# snapshot and automatic rollback are what a role declares `k8s_autodeploy:
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
