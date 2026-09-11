#!/usr/bin/env python3
"""`deploy.sh` and `deploy_locks.py` must order the service locks identically.

Both take `server-deploy-all.lock` first and then one lock per tag in sorted order, and taking
them in a DIFFERENT order is what makes two deploys deadlock: one holds `pihole` waiting for
`pi-peer-backup` while the other holds `pi-peer-backup` waiting for `pihole`. Neither side can
see the other's order, so nothing but this test compares them.

They do not sort the same way by default. `sort` follows the locale — in `en_US.UTF-8` it
ignores the hyphen, putting `pihole` before `pi-peer-backup` — while Python's `sorted` compares
code points, which puts `pi-peer-backup` first. `deploy.sh` therefore pins `LC_ALL=C`, and this
test is what keeps it pinned.

Run: uv run pytest ansible/tests/deploy/test_the_two_lock_orderings_agree.py
"""

import subprocess
import sys

import pytest
from _helpers import REPO

_DEPLOY_SH = REPO / "scripts" / "deploy.sh"
# The pair that disagrees, and the reason this test is not a formality. Both are live roles,
# and a census that stopped finding them would compare two orderings of nothing.
_DISAGREEING_PAIR = ("pi-peer-backup", "pihole")


def _declared_tags() -> list[str]:
    """Every deploy tag, from the enumeration `deploy.sh` itself runs for a full run."""
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts/deploy_tools/deploy_tags.py"), "list"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.splitlines() if line.strip()]


def _shell_sorted(tags: list[str]) -> list[str]:
    """What `deploy.sh`'s own pipeline makes of them, `LC_ALL=C` and all."""
    pipeline = next(
        line.strip()
        for line in _DEPLOY_SH.read_text().splitlines()
        if "sort -u" in line and "split_tags" in line
    )
    _, _, sort_cmd = pipeline.partition("| ")
    result = subprocess.run(
        ["bash", "-c", f"cat | {sort_cmd.rstrip(')')}"],
        input="\n".join(tags) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_the_census_contains_the_pair_the_two_sorts_disagree_on():
    """Non-vacuity: without these two, both orderings agree on everything and prove nothing."""
    tags = _declared_tags()
    missing = [tag for tag in _DISAGREEING_PAIR if tag not in tags]
    assert not missing, (
        f"{missing} are no longer deploy tags, so this test compares two orderings that agree "
        "by luck. Find the pair today's locale sorts differently and name it here."
    )


def test_both_deploy_paths_lock_the_real_tag_list_in_the_same_order():
    """The invariant: the wrapper's order and the deployer's are the same list."""
    tags = _declared_tags()
    assert _shell_sorted(tags) == sorted(set(tags)), (
        "deploy.sh and deploy_locks.py order the service locks differently, so two deploys "
        "sharing two services can each hold the lock the other is waiting for"
    )


def test_the_locale_default_is_what_would_break_it():
    """FLAGGED half: the same pipeline without `LC_ALL=C`, which is the state this pins.

    Skipped where the host has no locale that reorders them — the disagreement is the point,
    and asserting it on a host that cannot show it would be asserting nothing.
    """
    result = subprocess.run(
        ["bash", "-c", "LC_ALL=en_US.UTF-8 sort -u"],
        input="\n".join(_DISAGREEING_PAIR) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("no en_US.UTF-8 locale on this host")
    locale_order = [line for line in result.stdout.splitlines() if line.strip()]
    if locale_order == sorted(_DISAGREEING_PAIR):
        pytest.skip("this host's en_US.UTF-8 sorts the pair the way Python does")
    assert locale_order != sorted(_DISAGREEING_PAIR), (
        "the locale sort and Python's agree here, so LC_ALL=C is what keeps them agreeing"
    )
