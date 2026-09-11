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


def plan(
    narrow: Callable[[str, str, str, float], tuple[int, str]],
    config,
    target,
    setup_tags: set[str],
) -> BroadPlan:
    """What this broad tick applies: the setup plane's own tags, or the deploy plane's.

    Args:
        narrow: `narrow_deploy_plane`, or a test's stand-in for it.
        config: the tick's `Config`, for the checkout path.
        target: the tick's `TickTarget`, for the two commits bounding the range.
        setup_tags: `setup_tags_for(paths)`, non-empty for a setup-plane change.

    The setup arm is unchanged: its tags were already derived, by `setup_tags_for`.
    """
    if setup_tags:
        return BroadPlan("ansible/initial_setup.yml", sorted(setup_tags), True)
    return _deploy_plane(narrow, config, target)


def _deploy_plane(narrow, config, target) -> BroadPlan:
    """The deploy plane: a narrowed `--tags`, nothing at all, or the whole play."""
    playbook = "ansible/deploy.yml"
    try:
        rc, out = narrow(config.repo, target.local, target.origin, NARROW_TIMEOUT_S)
    except Exception as exc:
        return _full_run(playbook, f"{type(exc).__name__}: {exc}")
    # DECIDED: a full run on any doubt. Every way the derivation can be unsure — a variable
    # the play itself reads, a removed containers_list entry, a tag list covering most of
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
    return BroadPlan(playbook, tags, True)


def _full_run(playbook: str, reason: str) -> BroadPlan:
    """The fallback, logged every tick it is taken so the journal says why the play was whole."""
    log(f"narrow: cannot narrow ({reason}) — full {playbook}")
    return BroadPlan(playbook, [], True)
