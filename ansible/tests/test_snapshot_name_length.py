#!/usr/bin/env python3
"""Longhorn refuses to DELETE a snapshot whose name exceeds 63 bytes, so nothing may create one.

Longhorn 1.12 validates a snapshot delete by building a label selector that carries the
snapshot's own name as a label VALUE (`longhorn.io/linked-clone-source-snapshot`, part of that
release's linked-clone feature). Kubernetes caps a label value at 63 bytes, so a longer name
makes the selector invalid and the webhook denies the delete:

    admission webhook "validator.longhorn.io" denied the request: failed to check if snapshot
    autodeploy-qbittorrent-8a74b5b8-qbittorrent-config-20260822141923 is a linked-clone
    entrypoint: values[0][longhorn.io/linked-clone-source-snapshot]: Invalid value: "...":
    must be no more than 63 bytes

CREATION NEVER BUILDS THAT SELECTOR. So an over-long snapshot is created happily, and only
becomes a problem once retention catches up and something tries to prune it — at which point
every deploy fails, in a different role, with a message about linked clones that never mentions
name length. Measured 2026-08-22: 13 undeletable snapshots across 4 volumes, and the fourth
full deploy of the day died 78s in.

Three properties, and the second is the one that rots quietly:

1. NO SERVICE/CLAIM PAIR IN THIS REPO PRODUCES AN OVER-LONG NAME. Derived from the roles
   themselves, so a new service with a long name is caught here rather than in production.

2. THE TWO ROLES BUILD THE CLAIM SEGMENT IDENTICALLY. k8s/volume-snapshot names the snapshot;
   k8s/volume-revert reconstructs that name as a prefix to find it again. If they drift, the
   revert matches NOTHING and reports "no snapshot for this deploy" — a silent no-op, not a
   failure — so the recovery point exists and cannot be found.

3. THE PRUNE TOLERATES ONLY THIS ONE REJECTION. Snapshots created before the fix cannot be
   deleted at all, so the prune has to survive them; but a blanket ignore would also swallow a
   down webhook or an RBAC denial, turning "no recovery point was taken" into a green deploy.

Run: uv run pytest ansible/tests/test_snapshot_name_length.py
"""

import re
from pathlib import Path

import pytest
import yaml
from ansible.template import Templar, trust_as_template

ANSIBLE = Path(__file__).resolve().parents[1]
K8S_ROLES = ANSIBLE / "roles" / "k8s"
SNAPSHOT = K8S_ROLES / "volume-snapshot" / "tasks" / "claim.yml"
REVERT = K8S_ROLES / "volume-revert" / "tasks" / "claim.yml"

LIMIT = 63

# `now(utc=true, fmt='%Y%m%d%H%M%S')` — volume-snapshot/tasks/main.yml.
TOKEN_LEN = len("20260822141923")
# `git rev-parse --short=8`. It returns MORE than 8 when 8 are ambiguous, and the snapshot
# carries whatever it returned, so the headroom reported below matters as much as the pass.
SHA_LEN = 8


def _name_expression() -> str:
    """The Jinja k8s/volume-snapshot actually assigns to volume_snapshot_name.

    Read out of the role rather than restated here. A Python twin of this expression would keep
    passing after the role changed, which is the failure this whole file exists to prevent one
    layer down.

    The WHOLE assigned string is returned and templated as-is. Pulling the inner expression out
    of its `{{ }}` and re-wrapping it silently drops the literal text between the parts — an
    earlier draft of this file measured `autodeploy-home-assistant-...` at 60 bytes instead of
    71, because the leading `autodeploy-` is literal and was being thrown away. It passed on
    the exact name that broke production.
    """
    for task in yaml.safe_load(SNAPSHOT.read_text()):
        fact = task.get("ansible.builtin.set_fact") or {}
        if "volume_snapshot_name" in fact:
            return fact["volume_snapshot_name"]
    pytest.fail(f"no set_fact assigns volume_snapshot_name in {SNAPSHOT}")


def _snapshot_pairs() -> list[tuple[str, str]]:
    """(service, claim) for every role declaring k8s_autodeploy_snapshot_pvcs."""
    pairs = []
    for defaults in sorted(K8S_ROLES.glob("*/defaults/main.yml")):
        try:
            loaded = yaml.safe_load(defaults.read_text()) or {}
        except yaml.YAMLError:
            continue
        for claim in loaded.get("k8s_autodeploy_snapshot_pvcs") or []:
            pairs.append((defaults.parents[1].name, str(claim)))
    return pairs


def _name(service: str, claim: str) -> str:
    """Render the role's own expression through Ansible's templar, as the deploy does."""
    templar = Templar(
        loader=None,
        variables={
            "volume_snapshot_service": service,
            "volume_snapshot_claim": claim,
            "volume_snapshot_sha": {"stdout": "x" * SHA_LEN},
            "volume_snapshot_run_token": "9" * TOKEN_LEN,
        },
    )
    return str(templar.template(trust_as_template(_name_expression()))).strip()


def test_there_are_pairs_to_check():
    """A derivation that silently finds nothing would make every check below vacuous."""
    assert len(_snapshot_pairs()) >= 5, (
        "no k8s_autodeploy_snapshot_pvcs found — the derivation broke, and the length check "
        "below is passing because it is checking nothing."
    )


@pytest.mark.parametrize("service,claim", _snapshot_pairs(), ids=lambda v: str(v))
def test_snapshot_name_fits(service, claim):
    name = _name(service, claim)
    assert len(name.encode()) <= LIMIT, (
        f"{service}/{claim} produces a {len(name.encode())}-byte snapshot name ({name}), over "
        f"Longhorn's {LIMIT}-byte delete ceiling. It would be created fine and then be "
        "impossible to prune, failing every deploy once retention caught up. Shorten the "
        "service or claim name."
    )


def test_the_two_roles_build_the_claim_segment_identically():
    """The drift guard. Divergence here is silent — the revert just matches nothing."""
    pattern = re.compile(
        r"regex_replace\('\^' ~ \((\w+)_service \| regex_escape\) ~ '-', ''\) "
        r"\| default\(\1_claim, true\)"
    )
    snap = pattern.search(SNAPSHOT.read_text())
    revert = pattern.search(REVERT.read_text())
    assert snap, (
        f"{SNAPSHOT} no longer strips the service prefix from the claim segment"
    )
    assert revert, (
        f"{REVERT} no longer strips the service prefix from the claim segment"
    )
    assert snap.group(0).replace("volume_snapshot", "X").replace(
        "volume_revert", "X"
    ) == revert.group(0).replace("volume_snapshot", "X").replace(
        "volume_revert", "X"
    ), (
        "k8s/volume-snapshot and k8s/volume-revert build the claim segment differently. The "
        "revert reconstructs the snapshot's name as a prefix, so any difference makes it match "
        "nothing and report 'no snapshot for this deploy' instead of failing."
    )


def test_the_assert_guards_the_name_before_it_is_created():
    """Shortening fixes today's four; the assert is what stops a fifth arriving unnoticed."""
    tasks = yaml.safe_load(SNAPSHOT.read_text())
    guards = [
        t
        for t in tasks
        if isinstance(t.get("ansible.builtin.assert"), dict)
        and any(
            str(LIMIT) in str(c) for c in t["ansible.builtin.assert"].get("that", [])
        )
    ]
    assert guards, (
        f"no assert bounds the snapshot name at {LIMIT} bytes. Without it a new service with a "
        "long name is created successfully and only fails several deploys later, during a "
        "prune, with an error that never mentions name length."
    )


def test_the_prune_tolerates_only_the_length_rejection():
    tasks = yaml.safe_load(SNAPSHOT.read_text())
    prunes = [t for t in tasks if t.get("name", "").startswith("Prune snapshots")]
    assert len(prunes) == 1, (
        f"expected one prune task in {SNAPSHOT}, found {len(prunes)}"
    )
    failed_when = prunes[0].get("failed_when")
    assert isinstance(failed_when, list), (
        "the prune's failed_when must be a list of conditions. A bare `false` would swallow "
        "every prune failure — a down webhook, RBAC, a wedged finalizer — and report a deploy "
        "that took no recovery point as green."
    )
    assert any("must be no more than 63 bytes" in str(c) for c in failed_when), (
        "the prune no longer tolerates Longhorn's 63-byte delete rejection by message. "
        "Snapshots created before the naming fix cannot be deleted through the API at all, so "
        "without this every deploy fails on a backlog it can do nothing about."
    )
