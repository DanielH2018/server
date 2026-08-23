#!/usr/bin/env python3
"""Guards k8s/seed-volume's annotation short-circuit — the gate that skips the seed pod.

The role seeds 25 claims on every deploy and every one of them is already seeded, so the whole
steady-state cost was module executions concluding "nothing to do". The short-circuit reads a
`homelab.daniel-hunter.com/seeded` annotation off the PVC and skips the seed pod cycle.

Two properties have to hold, and neither is visible in a green deploy:

1. THE COPY MUST NOT RUN WHEN THE POD CYCLE IS SKIPPED. Skipping the pod also skips the marker
   check, so there is no marker reading to consult — and a skipped Ansible task still registers,
   as a dict with no `rc`. Any expression that consults the marker here resolves to "copy", and
   tars a long-gone Docker source over 25 live volumes. These tests evaluate the real
   expression, lifted out of the role, under exactly that state.

2. THE ANNOTATION KEY MUST AGREE ACROSS THREE SITES — the escaped jsonpath that reads it, the
   `kubectl annotate` that writes it, and k8s/volume-revert's strip. A typo in any one of them
   is silent: the read just never matches, the short-circuit never fires, and the only symptom
   is a deploy that is slower than it should be.

Run: uv run pytest ansible/tests/test_seed_volume_short_circuit.py
"""

import re

import pytest
import yaml
from ansible.plugins.filter.core import FilterModule
from ansible.template import AnsibleUndefined
from jinja2 import Environment
from _helpers import ANSIBLE


SEED = ANSIBLE / "roles" / "k8s" / "seed-volume" / "tasks" / "seed.yml"
REVERT = ANSIBLE / "roles" / "k8s" / "volume-revert" / "tasks" / "claim.yml"

ANNOTATION = "homelab.daniel-hunter.com/seeded"
# How that key has to appear inside `kubectl -o jsonpath=...`: dots escaped, slash bare.
JSONPATH_KEY = ANNOTATION.replace(".", r"\.")


def _expr(name: str) -> str:
    """The Jinja expression assigned to `name` by a set_fact in seed.yml, braces stripped.

    Read out of the role source rather than restated here, so the tests below exercise what
    actually ships. A restated copy would keep passing after the role changed.
    """
    text = SEED.read_text()
    match = re.search(
        rf"^\s*{re.escape(name)}:\s*(.*?)(?=\n\s*\w[\w_]*:|\n\n)", text, re.S | re.M
    )
    assert match, f"no set_fact assigns {name} in {SEED}"
    body = match.group(1)
    inner = re.search(r"\{\{(.*)\}\}", body, re.S)
    assert inner, f"{name}'s value is not a Jinja expression: {body!r}"
    return inner.group(1).strip()


def _render(expr: str, **ctx):
    """Evaluate one expression under Ansible's own filters and undefined type.

    Both borrowings are load-bearing rather than pedantry, and using Jinja's defaults instead
    makes these tests pass while failing to reproduce the deploy:

    `bool` — Ansible maps the STRING "False" to False where Python's builtin maps it to True.
    These facts are set through a folded YAML scalar, so a string is exactly what arrives.

    `AnsibleUndefined` — Jinja's StrictUndefined and Ansible's undefined disagree about what
    `marker.rc` does when the register has no `rc`, and the tests below turn on exactly that.
    Borrow Ansible's so the disagreement cannot be decided in the test's favour.
    """
    env = Environment(undefined=AnsibleUndefined)
    env.filters.update(FilterModule().filters())
    return env.from_string("{{ " + expr + " }}").render(**ctx)


# --------------------------------------------------------------------------- the copy decision

# A skipped task's register: present, but carrying no `rc`. This is the shape that makes a
# careless `| default(1)` resolve to "not seeded, therefore copy".
SKIPPED_REGISTER = {"changed": False, "skipped": True}


def test_copying_is_false_when_short_circuiting():
    """The whole safety property. Short-circuit means seeded, means nothing to copy."""
    result = _render(
        _expr("seed_volume_copying"),
        seed_volume_short_circuit=True,
        seed_volume_marker=SKIPPED_REGISTER,
        seed_volume_force=False,
    )
    assert result == "False", (
        f"seed_volume_copying rendered {result!r} with the seed pod skipped. That runs copy.yml, "
        "which tars the (long-gone) Docker source over a live Longhorn volume. The expression "
        "must return a hard false on the short-circuit branch, not fall through to the marker."
    )


def test_short_circuit_outranks_a_marker_that_says_copy():
    """The short-circuit must decide alone, not be OR'd with the marker.

    A marker of rc=1 means "no .seeded file, copy this volume". Feeding that in alongside the
    short-circuit is the poison test: if the expression consults the marker at all, the result
    flips to True. This is the guard that states the property as a VALUE. The skipped-register
    case above states it as "does not misbehave on a register with no rc", which catches the
    same mistakes but through whatever Ansible's undefined happens to do there.
    """
    result = _render(
        _expr("seed_volume_copying"),
        seed_volume_short_circuit=True,
        seed_volume_marker={"rc": 1},
        seed_volume_force=False,
    )
    assert result == "False", (
        f"seed_volume_copying rendered {result!r}: the marker is still being consulted on the "
        "short-circuit branch. The annotation is the decision there — the marker is unreadable "
        "without the seed pod that was just skipped."
    )


@pytest.mark.parametrize(
    ("rc", "force", "expected"),
    [
        (0, False, "False"),  # marker found, no force — the pre-existing no-op
        (1, False, "True"),  # no marker — a genuine first seed
        (0, True, "True"),  # marker found but force overrules it — the cutover case
        (1, True, "True"),
    ],
)
def test_copying_on_the_long_path_is_unchanged(rc, force, expected):
    """The behaviour that existed before the short-circuit must survive it."""
    result = _render(
        _expr("seed_volume_copying"),
        seed_volume_short_circuit=False,
        seed_volume_marker={"rc": rc},
        seed_volume_force=force,
    )
    assert result == expected, (
        f"marker rc={rc}, force={force} now decides copying={result}, expected {expected}. The "
        "short-circuit was meant to add a skip, not change what happens when it does not fire."
    )


# ------------------------------------------------------------------------------- the gate itself


@pytest.mark.parametrize(
    ("stdout", "force", "expected"),
    [
        ("pvc-abc|true", False, "True"),  # annotated and seeded
        ("pvc-abc|true", True, "False"),  # force always takes the long path
        ("pvc-abc|", False, "False"),  # bound but never annotated
        ("pvc-abc", False, "False"),  # no separator at all
        ("", False, "False"),  # no PVC yet — the first-seed case
    ],
)
def test_short_circuit_gate(stdout, force, expected):
    result = _render(
        _expr("seed_volume_short_circuit"),
        seed_volume_pv={"stdout": stdout},
        seed_volume_force=force,
    )
    assert result == expected, (
        f"stdout={stdout!r}, force={force} gave short_circuit={result}, expected {expected}."
    )


def test_force_defeats_the_short_circuit():
    """seed_volume_force exists to overrule the marker at a cutover — see test_seed_volume_force.

    A gate that outranked force would turn the one run that matters into a silent no-op, which
    is the same failure that test guards from the other side.
    """
    assert "seed_volume_force" in _expr("seed_volume_short_circuit")


# ------------------------------------------------------------------------------ the key agreement


def test_jsonpath_reads_the_key_the_annotate_writes():
    text = SEED.read_text()
    assert JSONPATH_KEY in text, (
        f"seed.yml has no jsonpath reading {ANNOTATION}. Inside `-o jsonpath=` the dots must be "
        rf"backslash-escaped ({JSONPATH_KEY}) or kubectl parses them as path separators and "
        "silently returns nothing — the short-circuit would then never fire."
    )
    assert f"{ANNOTATION}=true" in text, (
        f"seed.yml never annotates the PVC with {ANNOTATION}=true"
    )


def test_volume_revert_strips_the_annotation():
    """Reverse state: volume-revert replaces the volume's contents, which is what the key asserts.

    Stripping it unnecessarily costs one seed pod cycle on the next deploy, which re-reads the
    in-volume marker and re-annotates. Not stripping it after a revert that did land pre-seed
    would leave a volume permanently believed seeded when it is not.
    """
    assert f"{ANNOTATION}-" in REVERT.read_text(), (
        f"k8s/volume-revert never removes the {ANNOTATION} annotation. seed-volume skips its "
        "whole seed pod cycle on that key, so a reverted volume would keep claiming to be seeded."
    )


# ---------------------------------------------------------------------------- what must not skip


def test_the_pvc_render_is_never_short_circuited():
    """16 of the caller roles render no PVC template of their own; this role is their only one."""
    text = SEED.read_text()
    loop = re.search(r"loop: >-\s*\n\s*(\{\{.*?\}\})", text, re.S)
    assert loop, "the manifest render no longer uses a computed loop"
    for short_circuit in (True, False):
        rendered = _render(
            loop.group(1).strip("{} \n"), seed_volume_short_circuit=short_circuit
        )
        assert "pvc.yaml" in rendered, (
            f"pvc.yaml is not rendered when short_circuit={short_circuit}. seed-volume is the "
            "only thing that creates the PVC for bazarr, freshrss, sonarr and 13 others — "
            "skipping it deletes nothing but leaves those services with no claim to mount."
        )
        assert ("seed-pod.yaml" in rendered) is not short_circuit, (
            "seed-pod.yaml should be rendered exactly when a seed pod is going to start"
        )


def test_the_gates_producers_are_tagged_always():
    """`seed_volume_short_circuit` is the role's first fact read across the config/deploy split.

    Ansible unions tags for selection but excludes on any tag, and `set_fact` persists for the
    host across role invocations. So a `config`-tagged producer that gets filtered out does not
    leave the fact undefined — it leaves the PREVIOUS claim's value in scope. A stale `true`
    skips the seed pod for a claim that was never seeded, and its workload starts against an
    empty volume.
    """
    tasks = yaml.safe_load(SEED.read_text())
    producers = [
        task
        for task in tasks
        # By the set_fact's KEYS, not its rendered text: `Decide whether this run copies` reads
        # the gate inside its expression and would otherwise match as a producer of it.
        if "seed_volume_short_circuit" in (task.get("ansible.builtin.set_fact") or {})
        or task.get("register") == "seed_volume_pv"
    ]
    assert len(producers) == 2, (
        f"expected the lookup and the set_fact to produce the gate, found {len(producers)}"
    )
    for task in producers:
        assert task.get("tags") == "always", (
            f"{task['name']!r} is tagged {task.get('tags')!r}, not `always`. It produces the fact "
            "six deploy-tagged tasks gate on — see this test's docstring for what a stale value "
            "does."
        )
