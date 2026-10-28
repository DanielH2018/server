#!/usr/bin/env python3
"""Which of a setup role's own tags a broad tick applies.

`handle_broad` applies a setup role this host runs with `initial_setup.yml`. Since #3120 it
narrows each role tag to the block tags the range's own diff reaches, so a one-template change
does not re-run the whole role.

The derivation lives in `scripts/deploy_tools/narrow_setup.py` and is reached as a
SUBPROCESS, not imported: it parses YAML and this unit runs under `uv run --no-project`,
which cannot import yaml. `narrow_setup_role` is that boundary and is a `DeployTools`
field, so a test replaces it rather than a module attribute.

A deploy-plane path, under `ansible/templates/` or `ansible/inventory/`, gets no plan here.
The renderer reads those paths, so their cluster half reaches the cluster through `promote`,
and the Pi half stays a hand deploy (docs/target-architecture/deploy/d6-delete-old-path.md,
*The route of a deploy-plane path after D6*).

Reach `deploy_io` and `deploy_alerts` qualified, never by from-import.
"""

import subprocess
import time
from typing import Callable, NamedTuple

from deployer_lib.deploy_config import log

# The playbook the setup arm runs, and the one `narrow_setup.py` derives reachability against.
from deployer_lib.deploy_setup_roles import INITIAL_SETUP
from gitops_hold import held_tag

# What the tick's receipt records as a plane's tags when every setup role in the range answers
# that it needs no tag (#4361). The fast-forward IS the whole apply there, so the receipt has
# to say that an apply happened without naming tags that were never passed to anything.
NARROWED_TO_NOTHING = "narrowed-to-nothing"

# How long the setup-role narrowing gets, and the script that does it. One `git ls-tree` plus
# a `git show` per file of ONE role, so a run still going at thirty seconds has wedged.
NARROW_SETUP_TIMEOUT_S = 30.0
NARROW_SETUP_SCRIPT = "scripts/deploy_tools/narrow_setup.py"
# The exit code `narrow_setup.py` answers "this role needs no tag" with, beside
# `NARROWED_TO_NOTHING` on stdout (#4361). The script cannot be imported here, so it holds
# the same pair as `NOTHING_EXIT` and `NOTHING_OUT`; `scripts/deploy_tools/tests/
# test_narrow_setup.py` drives its CLI with this module's argv and pins the two together. The
# value is `lib/exit_codes.py:NARROW_SETUP_NOTHING`, which the deployer cannot import. It is 5
# rather than 3 because `narrow_cmd` exits 3 for "cannot narrow" (#4453).
NARROW_SETUP_NOTHING_EXIT = 5

# What the whole setup-narrowing LOOP gets, across every role one range touches. The per-role
# budget bounds one child; nothing bounds how many roles a range carries, and `plan` runs
# BEFORE the ff-merge and inside the unit's TimeoutStartSec, which the phase budgets in
# `defaults/main.yml` already fill. Three roles at the per-role budget is the shape measured
# ranges reach; past that the remaining roles take their whole-role tag, which is what they
# did before #3120.
NARROW_SETUP_TOTAL_BUDGET_S = 3 * NARROW_SETUP_TIMEOUT_S


class BroadPlan(NamedTuple):
    """What one broad tick applies.

    Attributes:
        playbook: the playbook to run.
        tags: its `--tags` value. Never empty on a plan that applies: an empty `--tags`
            runs the whole playbook.
        apply: False when there is nothing to run, and the ff-merge is the whole apply.
        hold_tags: what a FAILED apply holds, when it differs from what it ran. None
            means the two are the same, which is every plan but the narrowed setup plane.
            That plane holds each block tag qualified by its role (`gitops_hold.held_tag`).
    """

    playbook: str
    tags: list[str]
    apply: bool
    hold_tags: list[str] | None = None

    # The narrowed setup plane holds the block tags it RAN, each qualified by the role tag it
    # narrowed from: `gitops_deploy:gitops-config`, not `gitops-config` and not the whole
    # `gitops_deploy` (#3138). A bare block tag would survive the whole-role fallback that
    # reruns it, and a sticky `hold_sha` parks every session's landing. The whole role
    # over-claimed: a later apply narrowing to a different block of that role cleared it.
    @property
    def held(self) -> list[str]:
        """The tags a failed apply records in `hold_plane`."""
        return self.tags if self.hold_tags is None else self.hold_tags


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
        (exit code, stdout). Exit 0 with a comma-joined tag list narrows. Exit
        `NARROW_SETUP_NOTHING_EXIT` with `NARROWED_TO_NOTHING` says the role needs no tag,
        which only `narrowed_setup_tags` acts on. Anything else is a refusal, and the caller
        records no narrowing so every surface prints the role tag.

    A subprocess because the derivation parses YAML and this unit runs under
    `uv run --no-project`. `uv run --frozen` uses the repo's pinned env and never mutates
    uv.lock on the host. Every stderr line is logged, so the journal carries the per-path
    derivation the marker was written from.
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
            log(f"narrow-setup: {line.strip().removeprefix('narrow-setup: ')}")
    return r.returncode, r.stdout.strip()


def narrowed_setup_tags(
    narrow_setup: Callable[..., tuple[int, str]],
    config,
    target,
    setup_tags: set[str],
    setup_roles: dict[str, str],
    now: Callable[[], float] = time.monotonic,
) -> dict[str, list[str]]:
    """`setup_tags`, with each role tag replaced by the block tags its own diff reaches (#3120).

    Args:
        narrow_setup: `narrow_setup_role`, or a test's stand-in for it.
        config: the tick's `Config`, for the checkout path.
        target: the tick's `TickTarget`, for the two commits bounding the range.
        setup_tags: `setup_tags_for(paths)` — the whole-role tags this tick would apply.
        setup_roles: role tag -> role directory, for the roles `INITIAL_SETUP` applies.
        now: the monotonic clock the shared budget below is measured on. A parameter so a
            test can spend the budget without sleeping.

    Returns:
        Each tag of `setup_tags`, mapped to the sorted tags that role needs. An empty list is
        a role the derivation answered needs no tag (#4361), which the apply leaves out and
        a failed apply does not hold. The apply runs their union; a failed apply holds each
        one qualified by the key it came from. `--tags initial_setup` selects about 440
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
    out: dict[str, list[str]] = {}
    deadline = now() + NARROW_SETUP_TOTAL_BUDGET_S
    for tag in sorted(setup_tags):
        role = setup_roles.get(tag)
        if role is None:
            out[tag] = [tag]
            continue
        left = deadline - now()
        if left <= 0:
            out[tag] = sorted(
                _whole_role(role, tag, "this tick's narrowing budget is spent")
            )
            continue
        out[tag] = sorted(
            _one_setup_role(
                narrow_setup,
                config,
                target,
                role,
                tag,
                min(NARROW_SETUP_TIMEOUT_S, left),
            )
        )
    return out


def _one_setup_role(
    narrow_setup, config, target, role: str, role_tag: str, timeout: float
) -> set[str]:
    """One role's narrow tags, or `{role_tag}` with the refusal logged.

    DECIDED: the whole-role tag on any doubt.
    `narrow_setup.role_tags` already refuses on every ambiguity it can name — an untagged task
    file, a tag a second role declares, a derivation landing back on the role tag — and this
    adds the ones it cannot: a non-zero exit, empty output, and anything the subprocess layer
    raises. `except Exception` is deliberate: the call decodes a child's output, so UnicodeDecodeError is as reachable as SubprocessError, and
    `plan` runs BEFORE the ff-merge, where an escape parks every session's landing. A tag list
    that is too wide is only slow; one that is too narrow leaves the change unapplied behind a
    run that exited 0.

    One answer is not doubt, and it narrows this marker (#4361): exit
    `NARROW_SETUP_NOTHING_EXIT` with stdout `NARROWED_TO_NOTHING` says every changed path
    reaches no host through the role, so the role is dropped from the run. It takes both
    halves because the script runs from the pre-merge checkout while this code is the copy
    under /opt, so either can be the older one: a deployer that predates the answer reads the
    non-zero exit as a refusal, and the same exit code from anything else lacks the stdout.
    Exit 0 with empty output stays doubt.
    """
    try:
        rc, out = narrow_setup(
            config.repo,
            role,
            role_tag,
            INITIAL_SETUP,
            target.local,
            target.origin,
            timeout,
        )
    except Exception as exc:
        return _whole_role(role, role_tag, f"{type(exc).__name__}: {exc}")
    if rc == NARROW_SETUP_NOTHING_EXIT and out == NARROWED_TO_NOTHING:
        log(f"narrow-setup: {role} needs no tag over this range, so not applying it")
        return set()
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
    """The fallback, logged every tick it is taken."""
    log(f"narrow-setup: cannot narrow {role} ({reason}) — applying --tags {role_tag}")
    return {role_tag}


def plan(
    config,
    target,
    setup_tags: set[str],
    narrow_setup: Callable[..., tuple[int, str]],
    setup_roles: dict[str, str],
) -> list[BroadPlan]:
    """What this broad tick applies: the setup plane's narrowed tags, or nothing.

    Args:
        config: the tick's `Config`, for the checkout path.
        target: the tick's `TickTarget`, for the two commits bounding the range.
        setup_tags: `setup_tags_for(paths)`, non-empty for a setup-plane change.
        narrow_setup: `narrow_setup_role`, or a test's stand-in, for the setup arm.
        setup_roles: role tag -> role directory, for the roles `INITIAL_SETUP` applies.

    The setup arm narrows since #3120. `setup_tags_for` derives WHICH role tag each path
    needs, and `narrowed_setup_tags` then narrows each of those to the role's own block tags.
    A deploy-plane path adds no plan: the tick fast-forwards past it.
    """
    if not setup_tags:
        return []
    by_role = narrowed_setup_tags(narrow_setup, config, target, setup_tags, setup_roles)
    tags = sorted({t for role_tags in by_role.values() for t in role_tags})
    held = sorted(held_tag(r, t) for r, role_tags in by_role.items() for t in role_tags)
    if tags:
        return [BroadPlan(INITIAL_SETUP, tags, True, held)]
    # Every role answered that it needs no tag (#4361). An empty `--tags` value runs the
    # whole playbook, so this applies nothing and the receipt says so.
    log("narrow-setup: no setup role needs a tag, so merging and applying nothing")
    return [BroadPlan(INITIAL_SETUP, [NARROWED_TO_NOTHING], False)]
