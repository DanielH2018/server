"""The n8n Deployments stamp the registry digest they were rendered for.

`k8s_rebuilt_images` rolls a pod when an image's digest MOVES during a play. It cannot see a
registry that is already ahead of the running pod, which is what a run that pushes an image and
then dies before the rollout leaves behind — the re-run rebuilds to the same digest, records no
change, and the pod keeps the old image behind a green deploy. The `checksum/image` annotation
covers that: it puts the digest in the pod spec, so the RENDER differs whenever the registry and
the last render disagree.

The pair below is what proves the stamp can go red. A stamp that always renders the same string
would satisfy "the annotation is present" while rolling nothing, so the second test asserts that a
DIFFERENT digest produces a different manifest.
"""

from __future__ import annotations

import sys

import yaml
from _helpers import REPO

sys.path.insert(0, str(REPO / "scripts"))

from validate.k8s_manifests import (
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    K8S_ROLES,
    SHARED_TPL,
    k8s_entries,
    load_yaml,
    make_env,
    make_lookup,
    register_ansible_filters,
    render_or_error,
    resolve_vars,
    role_defaults,
)

ROLE = "n8n"
# The two Deployments the n8n role ships, and the image each one runs. They differ: the app image
# is named after the role, the runners image is not — the same asymmetry that left a runners-only
# rebuild rolling nothing before manifests_extra_rollouts existed.
TEMPLATES = {
    "deployment.yaml.j2": "n8n",
    "deployment-runners.yaml.j2": "n8n-runners",
}


def _render(template: str, built_images: list[dict] | None) -> dict:
    role_dir = K8S_ROLES / ROLE
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), "playbook_dir": str(ANSIBLE)}
    base = resolve_vars(base, base)
    ctx = {
        **base,
        **role_defaults(ROLE, base),
        "container_item": k8s_entries()[ROLE],
    }
    if built_images is not None:
        ctx["k8s_built_images"] = built_images
    env = make_env([role_dir / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    rendered, err = render_or_error(env, template, ctx)
    assert rendered is not None, f"{template} failed to render: {err}"
    return yaml.safe_load(rendered)


def _stamp(doc: dict) -> str:
    return doc["spec"]["template"]["metadata"]["annotations"]["checksum/image"]


def test_each_deployment_stamps_the_digest_recorded_for_its_own_image():
    built = [
        {"name": "n8n", "digest": "sha256:aaa"},
        {"name": "n8n-runners", "digest": "sha256:bbb"},
    ]
    stamps = {t: _stamp(_render(t, built)) for t in TEMPLATES}

    assert stamps["deployment.yaml.j2"] == "sha256:aaa"
    assert stamps["deployment-runners.yaml.j2"] == "sha256:bbb"
    # Each Deployment reads its OWN image's entry. Both reading the same one would still pass a
    # "the annotation is present" check while leaving one of the two pods unrollable.
    assert stamps["deployment.yaml.j2"] != stamps["deployment-runners.yaml.j2"]


def test_a_moved_digest_changes_the_rendered_manifest():
    for template, image in TEMPLATES.items():
        before = _render(template, [{"name": image, "digest": "sha256:aaa"}])
        after = _render(template, [{"name": image, "digest": "sha256:ccc"}])
        assert _stamp(before) != _stamp(after), (
            f"{template} renders the same manifest for two different registry digests, so a "
            f"rebuild of {image} would queue no rollout"
        )


def test_an_absent_fact_stamps_a_stable_placeholder():
    # A config-only run, `--skip-tags deploy`, and every validator that renders this file outside
    # a play all leave the fact unset. Rendering a timestamp, or an empty value that later becomes
    # non-empty, would roll the pod on each of them.
    for template in TEMPLATES:
        assert _stamp(_render(template, None)) == "unstaged"
        assert _stamp(_render(template, [])) == "unstaged"
