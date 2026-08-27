"""A built image must be proven to have reached a pod, not just to have reached the registry.

Built images are pushed to a MUTABLE tag, so a rebuild changes what `:latest` resolves to while
leaving the Deployment spec byte-identical. Nothing in the spec changed, so nothing rolls, and the
new image sits in the registry unused behind a green deploy.

Hit for real 2026-08-27, on nut. Run 1 built the new base, pushed it, then died at "Read the build
result" — before the digest comparison that queues the rollout. Run 2 short-circuited the build,
because the rendered context was byte-identical AND the registry already served the tag, so it
recorded no change and queued nothing. The pod ran a week-old image for 90 minutes behind a 1/1
Deployment, a clean rollout and a passing `probe.py health`.

WHAT THIS FILE PROMISES
-----------------------
It is a SHAPE check, not a correctness test. It cannot run the gate — that needs a cluster, a
registry and running pods. It fails when the gate is removed, unwired, or quietly narrowed to the
one case it was NOT written for. Read it as a guard against silent removal; the gate's runtime
behaviour is proven by a real deploy, not by this file.

The load-bearing invariant is the last test. `Read the post-build digest` is deliberately NOT gated
on `image_builder_building`, unlike the build and every failure path around it. That asymmetry is
what lets a SKIPPED build still contribute a digest, which is the only reason case (2) above is
visible at all. To a reviewer it reads as an inconsistency, and the obvious tidy-up — adding the
`when:` for consistency with its four siblings — blinds the gate on exactly the path that hid the
failure. Hence a test rather than a comment.
"""

import re
from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[1]
_GATE = _ANSIBLE / "post_tasks" / "k8s_image_drift_gate.yml"
_DEPLOY = _ANSIBLE / "deploy.yml"
_BUILDER = _ANSIBLE / "roles" / "k8s" / "image-builder" / "tasks" / "main.yml"


def _tasks(path):
    """Every task in a task file, as dicts."""
    return [t for t in yaml.safe_load(path.read_text()) if isinstance(t, dict)]


def _k8s_play():
    """The k8s play, selected by name — deploy.yml opens with the Docker play for the Pi."""
    plays = yaml.safe_load(_DEPLOY.read_text())
    for play in plays:
        if play.get("name") == "Deploy k8s workloads":
            return play
    raise AssertionError("deploy.yml has no play named 'Deploy k8s workloads'")


def test_the_gate_exists_and_asserts_something():
    """A gate that renders but asserts nothing passes every deploy while proving nothing."""
    assert _GATE.exists(), (
        f"{_GATE} is missing — the built-image drift gate has been deleted"
    )
    modules = {
        m for task in _tasks(_GATE) for m in task if m.startswith("ansible.builtin.")
    }
    assert "ansible.builtin.assert" in modules, (
        f"{_GATE.name} no longer contains an assert. A gate whose failure path has been removed "
        "reports success on a stale pod, which is the exact state it exists to catch."
    )


def test_the_gate_is_wired_into_the_play():
    """An unincluded post_task is dead code that still passes its own unit test."""
    play = _k8s_play()
    included = {
        t.get("ansible.builtin.include_tasks")
        for t in play.get("post_tasks", [])
        if isinstance(t, dict)
    }
    assert "post_tasks/k8s_image_drift_gate.yml" in included, (
        "deploy.yml no longer includes post_tasks/k8s_image_drift_gate.yml. The gate only runs as a "
        "post_task — nothing else in the play reaches it."
    )


def test_the_gate_runs_after_the_stabilisation_gate():
    """Ordering is load-bearing: a pod mid-replacement has not finished resolving its image."""
    play = _k8s_play()
    order = [
        t.get("ansible.builtin.include_tasks")
        for t in play.get("post_tasks", [])
        if isinstance(t, dict)
    ]
    assert order.index("post_tasks/k8s_image_drift_gate.yml") > order.index(
        "post_tasks/k8s_stabilise_gate.yml"
    ), (
        "The drift gate must run AFTER the stabilisation gate. Comparing a running pod against the "
        "registry while its replacement is still rolling compares the outgoing pod."
    )


def test_the_play_resets_the_accumulator():
    """Facts persist across plays and resumed runs — a leftover entry is a false failure."""
    play = _k8s_play()
    reset = [
        t["ansible.builtin.set_fact"]
        for t in play.get("pre_tasks", [])
        if isinstance(t, dict) and "ansible.builtin.set_fact" in t
    ]
    assert any("k8s_built_images" in facts for facts in reset), (
        "deploy.yml must reset k8s_built_images in pre_tasks, alongside k8s_pending_rollouts and "
        "k8s_stabilise_watch. Without it a --tags run inherits images this play never built and "
        "asserts their stale digests against pods it has no business inspecting."
    )


def test_the_builder_records_every_image_not_only_changed_ones():
    """Keying the accumulator on a digest CHANGE would blind the gate to the case that hides.

    `k8s_rebuilt_images` is deliberately conditional — it drives the rollout decision. The drift
    accumulator must NOT be, because the pod it catches is stale from an EARLIER run, on which this
    run's digest legitimately does not move.
    """
    record = [
        t
        for t in _tasks(_BUILDER)
        if "k8s_built_images" in str(t.get("ansible.builtin.set_fact", ""))
    ]
    assert record, (
        "k8s/image-builder no longer records k8s_built_images. post_tasks/k8s_image_drift_gate.yml "
        "loops over that fact and silently checks nothing without it."
    )
    for task in record:
        conditions = task.get("when", [])
        conditions = [conditions] if isinstance(conditions, str) else conditions
        assert not any("digest_before" in str(c) for c in conditions), (
            "The drift accumulator has been made conditional on a digest change. That is what "
            "k8s_rebuilt_images already does, and it cannot see a pod left stale by an earlier "
            "run — the half of the 2026-08-27 nut failure that hid for 90 minutes."
        )


def test_the_post_build_digest_read_is_not_gated_on_the_build_running():
    """The asymmetry the gate rests on. See this module's docstring.

    A reviewer tidying `Read the post-build digest` into line with its four gated siblings removes
    the only signal a short-circuited build produces.
    """
    read = [
        t
        for t in _tasks(_BUILDER)
        if str(t.get("name", "")).startswith("Read the post-build digest")
    ]
    assert len(read) == 1, (
        "expected exactly one 'Read the post-build digest' task in k8s/image-builder"
    )
    conditions = read[0].get("when", [])
    conditions = [conditions] if isinstance(conditions, str) else conditions
    assert not any("image_builder_building" in str(c) for c in conditions), (
        "'Read the post-build digest' has been gated on image_builder_building. It reads the "
        "registry, not the Job, so it is safe on a skipped build — and running it there is the only "
        "reason the drift gate can see a stale pod behind an unchanged tag. See the DECIDED marker "
        "at that task."
    )


def test_the_gate_compares_against_the_running_pods_image_id():
    """imageID, not the Deployment's image: the named image is `:latest` either way."""
    body = _GATE.read_text()
    assert "imageID" in body, (
        "The gate no longer reads .status.containerStatuses[].imageID. A Deployment's image field "
        "names a mutable tag, so it matches whether or not the pod picked the new image up — only "
        "a running container reports the digest it actually resolved."
    )
    assert re.search(r"deletionTimestamp", body), (
        "The gate no longer excludes terminating pods. A pod with deletionTimestamp set still "
        "reports phase: Running, so a slow terminationGracePeriodSeconds would fail the deploy on "
        "the outgoing pod."
    )


def _evaluate_gate(pods, built_image):
    """Evaluate the gate's REAL assert expression against fake pod JSON.

    Everything above is a shape check. This is not: it lifts the condition straight out of the task
    file and runs it, so a Jinja bug fails here instead of on a live deploy.

    It exists because the first version of this gate shipped with one and reached master. The
    expression reached the pod list with `.items`, and Jinja resolves a dotted name to the ATTRIBUTE
    before the key — `items` is a Python dict method, so `rejectattr` was handed a bound method and
    every image-building deploy died with "'method' object is not iterable". Seven structural tests
    and a full `prek run` passed over it, because none of them evaluated the template.
    """
    import json

    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.test.core import TestModule
    from jinja2 import Environment

    task = next(
        t for t in _tasks(_GATE) if "ansible.builtin.assert" in t and "loop" in t
    )
    expression = task["ansible.builtin.assert"]["that"][0]

    env = Environment()
    env.filters.update(FilterModule().filters())
    # `search` is an ansible TEST, not a filter — selectattr('imageID', 'search', ...) needs it.
    env.tests.update(TestModule().tests())
    rendered = env.from_string("{{ " + expression + " }}").render(
        item=built_image,
        k8s_image_drift_pods={"stdout": json.dumps(pods)},
        k8s_registry_pull_host="localhost:5000",
    )
    return rendered == "True"


_NUT = {"name": "nut", "digest": "sha256:aaa"}


def _pod(image_id, phase="Running", deleting=False):
    meta = {"name": "nut-1"}
    if deleting:
        meta["deletionTimestamp"] = "2026-08-27T15:00:00Z"
    return {
        "metadata": meta,
        "status": {"phase": phase, "containerStatuses": [{"imageID": image_id}]},
    }


def test_the_gate_passes_when_the_pod_matches_the_registry():
    assert _evaluate_gate({"items": [_pod("localhost:5000/nut@sha256:aaa")]}, _NUT)


def test_the_gate_fails_when_the_pod_is_stale():
    """The whole point: registry moved, pod did not."""
    assert not _evaluate_gate({"items": [_pod("localhost:5000/nut@sha256:bbb")]}, _NUT)


def test_the_gate_ignores_a_terminating_pod():
    """A pod being replaced still reports phase: Running, and its digest is legitimately old."""
    assert _evaluate_gate(
        {"items": [_pod("localhost:5000/nut@sha256:bbb", deleting=True)]}, _NUT
    )


def test_the_gate_ignores_another_images_pod():
    """Image name is matched, not workload name — n8n-images builds what the n8n role deploys."""
    assert _evaluate_gate({"items": [_pod("localhost:5000/other@sha256:bbb")]}, _NUT)


def test_the_gate_passes_when_no_pod_runs_the_image():
    """Build-only images and scaled-to-zero workloads pass, deliberately."""
    assert _evaluate_gate({"items": []}, _NUT)


def test_the_gate_skips_an_unreadable_registry_digest():
    """An empty digest is not compared — upstream already treats it as 'changed'."""
    assert _evaluate_gate(
        {"items": [_pod("localhost:5000/nut@sha256:bbb")]},
        {"name": "nut", "digest": ""},
    )
