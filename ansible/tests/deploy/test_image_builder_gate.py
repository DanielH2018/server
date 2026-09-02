#!/usr/bin/env python3
"""Guards k8s/image-builder's build gate — the fact that decides whether an image is rebuilt.

Seven images are built in-cluster and every one of them rebuilt on every full deploy, costing
~106s for a result that was byte-identical five times out of seven (measured 2026-08-22 across
two full deploys five days apart). `image_builder_building` skips the build when the rendered
context has not changed and the registry already serves the tag.

The gate's two failure directions are not symmetric, and that asymmetry is what these tests
encode. A needless build costs ~15s and is visible in the log. A wrongly skipped one ships a
stale image indefinitely, reports success, and is invisible in a green deploy — so every clause
that cannot decide must resolve toward BUILDING.

Three properties have to hold:

1. THE GATE RESOLVES TOWARD BUILDING WHEN IT CANNOT TELL. An undefined or skipped render
   register, and a registry that answers anything other than 200, all mean build.

2. EVERY BUILD-PATH TASK CARRIES BOTH CLAUSES — the gate, and the separate `not k8s_no_mutate`
   dry-run guard the gate deliberately does not absorb. Dropping the first rebuilds everything
   again; dropping the second lets a dry run build and push an image.

3. THE TWO CONSUMERS THAT DEREFERENCE THE BUILD RESULT ARE GUARDED BEFORE THEY DEREFERENCE IT.
   Ansible short-circuits a `when` list, and a skipped task still registers a dict with no
   `stdout`, so the ordering is what keeps `image_builder_result.stdout` from being read.

Run: uv run pytest ansible/tests/deploy/test_image_builder_gate.py
"""

import re

import pytest
import yaml
from ansible.template import Templar, trust_as_template
from _helpers import ANSIBLE
from _helpers import load_tasks


ROLE = ANSIBLE / "roles" / "k8s" / "image-builder"
TASKS = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"

GATE = "image_builder_building"

# The tasks that must not run when the gate is false. Named rather than derived, because the
# point is to state the intended set: a task added to the build path and not added here is
# caught by test_no_build_path_task_still_uses_the_old_clause below.
BUILD_PATH = [
    "Apply the build context",
    "Clear the previous build",
    "Build and push",
    "Wait for the build to finish",
    "Read the build result",
]

# The two that dereference `image_builder_result.stdout`, which a skipped build never sets.
DEREFERENCING = [
    "Show the failed build log",
    "Fail on an unsuccessful build",
]


def _tasks():
    return load_tasks(TASKS)


def _task(prefix: str):
    matches = [t for t in _tasks() if t.get("name", "").startswith(prefix)]
    assert len(matches) == 1, f"{prefix!r} matched {len(matches)} tasks in {TASKS}"
    return matches[0]


def _gate_expression() -> str:
    """The Jinja expression the role assigns to the gate, braces stripped.

    Read out of the role source rather than restated here, so the tests below exercise what
    actually ships. A restated copy would keep passing after the role changed.
    """
    for task in _tasks():
        fact = task.get("ansible.builtin.set_fact") or {}
        if GATE in fact:
            inner = re.search(r"\{\{(.*)\}\}", fact[GATE], re.S)
            assert inner, f"{GATE} is not a Jinja expression: {fact[GATE]!r}"
            return inner.group(1).strip()
    pytest.fail(f"no set_fact assigns {GATE} in {TASKS}")


def _render(**ctx) -> str:
    """Evaluate the gate through Ansible's own templar, exactly as the deploy does.

    A hand-built Jinja Environment is not a substitute, and the difference is not cosmetic:
    Ansible's undefined raises `ReferenceError: A required TemplateContext context is not
    active.` on attribute access outside a real template context, so `default()` never gets the
    chance to catch a missing key. Every clause in this gate turns on exactly that — reading
    `.skipped` off a register that has no such key is the steady-state path.
    """
    templar = Templar(loader=None, variables=dict(ctx))
    return str(templar.template(trust_as_template("{{ " + _gate_expression() + " }}")))


# A register that is present but carries no result, which is what a skipped task leaves behind.
SKIPPED_REGISTER = {"changed": False, "skipped": True}

# Steady state: the context rendered identically and the registry already serves the tag.
STEADY = dict(
    k8s_no_mutate=False,
    image_builder_force=False,
    image_builder_digest_before={"status": 200},
    image_builder_render={"changed": False},
)


def test_steady_state_skips_the_build():
    """The saving. Nothing changed and the image exists, so there is nothing to rebuild."""
    assert _render(**STEADY) == "False", (
        "the gate fires on an unchanged context, so no build is ever skipped and the ~106s "
        "this role's gate exists to save is still being spent on every deploy."
    )


def test_a_changed_context_builds():
    assert _render(**{**STEADY, "image_builder_render": {"changed": True}}) == "True"


def test_a_missing_image_builds():
    """404 is the registry saying it has never seen this tag, or has lost it."""
    assert (
        _render(**{**STEADY, "image_builder_digest_before": {"status": 404}}) == "True"
    ), (
        "an image absent from the registry must be built regardless of the context, or a "
        "wiped registry never refills and every consuming Deployment fails to pull."
    )


def test_force_builds_an_unchanged_context():
    """The base-image CVE escape hatch: `FROM alpine:3.24` moves, the context does not."""
    assert _render(**{**STEADY, "image_builder_force": True}) == "True"


def test_force_accepts_the_string_an_extra_var_delivers():
    """`-e image_builder_force=true` arrives as a string, not a bool."""
    assert _render(**{**STEADY, "image_builder_force": "true"}) == "True", (
        "the documented invocation is an extra-var, which Ansible delivers as a string. A gate "
        "that only honours a real bool leaves the operator with no way to force a rebuild."
    )


@pytest.mark.parametrize(
    "render",
    [
        pytest.param(SKIPPED_REGISTER, id="skipped"),
        pytest.param({}, id="empty"),
    ],
)
def test_an_undecidable_render_register_builds(render):
    """A register that did not record a comparison cannot be read as 'unchanged'.

    A skipped task registers `changed: false`, which is indistinguishable from a real
    no-change reading by that key alone — hence the explicit `skipped` clause.
    """
    assert _render(**{**STEADY, "image_builder_render": render}) == "True", (
        "a render that did not run resolved to 'nothing changed', which skips the build "
        "forever: the context is never compared, so the flag never flips back."
    )


def test_an_undefined_render_register_builds():
    ctx = {k: v for k, v in STEADY.items() if k != "image_builder_render"}
    assert _render(**ctx) == "True"


def test_an_unreadable_digest_builds():
    """No `status` key at all — the safe reading is 'the registry did not confirm the tag'."""
    assert _render(**{**STEADY, "image_builder_digest_before": {}}) == "True"


def test_the_gate_does_not_absorb_the_dry_run_guard():
    """The two clauses stay separate, and this is the test that keeps them that way.

    Folding `not k8s_no_mutate` into the gate is correct and tempting — one fact instead of two
    clauses per task. It is still wrong, because `ansible/tests/deploy/test_k8s_dry_run.py` reads that
    guard textually at each mutating task, and a guard behind an indirection is one that check
    cannot see. Keeping them separate is what leaves the dry-run protection legible.
    """
    assert "k8s_no_mutate" not in _gate_expression(), (
        "the gate absorbed the dry-run guard. That makes every build-path task's protection "
        "invisible to test_k8s_dry_run.py, which then stops catching the next role to omit it."
    )


def _clauses(prefix) -> list[str]:
    when = _task(prefix)["when"]
    return [str(c) for c in (when if isinstance(when, list) else [when])]


@pytest.mark.parametrize("prefix", BUILD_PATH)
def test_build_path_tasks_carry_both_clauses(prefix):
    """One clause says a build is needed, the other says a build is permitted. Both required."""
    clauses = _clauses(prefix)
    assert any(GATE in c for c in clauses), (
        f"{prefix!r} is not gated on {GATE}, so it rebuilds an image whose context has not "
        "changed — the whole cost this gate exists to remove."
    )
    assert any("k8s_no_mutate" in c for c in clauses), (
        f"{prefix!r} lost its dry-run guard, so --check and --dry-run now build and push an "
        "image. That is the most mutating thing in this play."
    )


@pytest.mark.parametrize("prefix", DEREFERENCING)
def test_dereferencing_consumers_are_guarded_before_the_deref(prefix):
    """Ordering is the whole protection here, not merely tidiness.

    Ansible short-circuits a `when` list. A skipped build leaves `image_builder_result` a dict
    with no `stdout`, so both guards have to be evaluated before the clause that reads it —
    otherwise the play fails on an undefined attribute instead of skipping.
    """
    clauses = _clauses(prefix)
    deref = next(
        (i for i, c in enumerate(clauses) if "image_builder_result" in c),
        None,
    )
    assert deref is not None, f"{prefix!r} no longer reads image_builder_result"
    guards = [i for i, c in enumerate(clauses) if GATE in c or "k8s_no_mutate" in c]
    assert guards and max(guards) < deref, (
        f"{prefix!r} reads image_builder_result.stdout at clause {deref} with guards at "
        f"{guards}. Both guards must precede it, or a skipped build dereferences an absent "
        "key and fails the play with a message that names neither cause."
    )


def test_force_is_documented_as_a_default():
    """An escape hatch nobody can find is not an escape hatch."""
    defaults = yaml.safe_load(DEFAULTS.read_text())
    assert defaults.get("image_builder_force") is False, (
        "image_builder_force must default to false in defaults/main.yml — that file is where "
        "the role's contract is readable in one place, and the CVE case is explained there."
    )
