#!/usr/bin/env python3
"""jellyfin's snapshot-space cap must stay a value Longhorn will accept.

Longhorn's `Volume.Spec.snapshotMaxSize` takes "0" (uncapped) or a value no smaller than
`Volume.Spec.Size` x 2 — "because Longhorn requires at least two snapshots to function
properly" (longhorn.io/docs/1.12.1, Snapshot Space Management). jellyfin declares a cap, so its
value and `jellyfin_k8s_size` are coupled, and the coupling only breaks in one direction: raise
the PVC past half the cap and the admission webhook refuses the patch on EVERY jellyfin deploy
from then on.

`jellyfin_k8s_snapshot_max_size` is therefore derived from `jellyfin_k8s_size` in the role
defaults rather than written out. This file EVALUATES that expression instead of
pattern-matching it, so a rewritten-but-still-correct expression passes and a
rewritten-and-wrong one does not.

Run: uv run pytest ansible/tests/longhorn/test_jellyfin_snapshot_max_size.py
"""

import jinja2
import pytest

from lib import yaml_fast
from _helpers import ANSIBLE

DEFAULTS = ANSIBLE / "roles" / "k8s" / "jellyfin" / "defaults" / "main.yml"
TASKS = ANSIBLE / "roles" / "k8s" / "jellyfin" / "tasks" / "main.yml"

GIB = 1024**3


def _defaults() -> dict:
    return yaml_fast.safe_load(DEFAULTS.read_text())


def _resolve(expression: str, size: str) -> int:
    return int(
        jinja2.Environment().from_string(expression).render(jellyfin_k8s_size=size)
    )


def _assert_cap_is_legal(expression: str, size: str) -> None:
    """The invariant Longhorn's admission webhook enforces, checked here instead."""
    floor = int(size.removesuffix("Gi")) * GIB * 2
    resolved = _resolve(expression, size)

    assert resolved >= floor, (
        f"jellyfin_k8s_snapshot_max_size resolves to {resolved} bytes against a "
        f"jellyfin_k8s_size of {size} ({floor} bytes is the floor).\nLonghorn refuses a "
        f"non-zero snapshotMaxSize below twice the volume size, so this value would fail the "
        f"patch in tasks/main.yml on every jellyfin deploy."
    )
    assert resolved != 0, (
        "jellyfin_k8s_snapshot_max_size resolves to 0, which is Longhorn's UNCAPPED value — "
        "the pre-apply snapshot chain could then grow the backend without bound, which is the "
        "thing this var exists to stop."
    )


def test_the_pvc_size_is_still_expressed_in_gi():
    """The unit the derivation slices off, asserted rather than assumed.

    `jellyfin_k8s_snapshot_max_size` reads `jellyfin_k8s_size[:-2]` and multiplies by 1024**3.
    Rewriting the size as `8192Mi` would leave that arithmetic silently wrong, and no other
    assertion in this file would notice — both values move together.
    """
    size = _defaults()["jellyfin_k8s_size"]

    assert size.endswith("Gi") and size.removesuffix("Gi").isdigit(), (
        f"jellyfin_k8s_size is {size!r}. jellyfin_k8s_snapshot_max_size derives itself by "
        f"stripping a two-character `Gi` suffix — change the unit and the derivation must "
        f"change with it."
    )


def test_the_declared_cap_is_legal_for_the_declared_size():
    defaults = _defaults()
    _assert_cap_is_legal(
        defaults["jellyfin_k8s_snapshot_max_size"], defaults["jellyfin_k8s_size"]
    )


def test_the_cap_stays_legal_when_the_pvc_is_raised():
    """The drift this guard actually exists for.

    A hardcoded cap passes the test above and fails the moment someone raises the PVC. The
    sizes below span the raise that already happened (3Gi to 8Gi) and two beyond it.
    """
    expression = _defaults()["jellyfin_k8s_snapshot_max_size"]
    for size in ("3Gi", "8Gi", "16Gi", "64Gi"):
        _assert_cap_is_legal(expression, size)


@pytest.mark.parametrize(
    ("what", "expression", "size"),
    [
        ("a hardcoded cap", "17179869184", "64Gi"),
        (
            "a cap derived at 1x instead of 2x",
            "{{ (jellyfin_k8s_size[:-2] | int) * 1024 * 1024 * 1024 }}",
            "8Gi",
        ),
        ("Longhorn's uncapped sentinel", "0", "8Gi"),
    ],
)
def test_the_guard_rejects_a_cap_longhorn_would_refuse(what, expression, size):
    """The red half: each of these is a plausible way to write the var, and wrong.

    The hardcoded case is checked at 64Gi, the size at which it stops being legal — it passes
    at today's 8Gi, which is exactly why the drift test above varies the size.
    """
    with pytest.raises(AssertionError):
        _assert_cap_is_legal(expression, size)


def test_the_deploy_actually_applies_the_cap():
    """A var nothing reads is a decision recorded and never enforced."""
    tasks = TASKS.read_text()

    assert "jellyfin_k8s_snapshot_max_size" in tasks, (
        "jellyfin/tasks/main.yml no longer reads jellyfin_k8s_snapshot_max_size — the cap is "
        "declared in defaults and applied to nothing, so the live volume stays uncapped"
    )
    assert "snapshotMaxSize" in tasks, (
        "jellyfin/tasks/main.yml no longer patches spec.snapshotMaxSize on the Longhorn volume"
    )
