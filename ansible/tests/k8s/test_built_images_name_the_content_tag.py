#!/usr/bin/env python3
"""Every in-cluster-built image pin names the content tag, not the mutable `:latest` alias.

WHY THIS EXISTS. k8s/image-builder pushes two names for one manifest: the mutable `:latest`
and an immutable `sha-<12 hex>` hashed over the build inputs, published to the play as
`k8s_built_image_tags`. A consumer reads it as `k8s_built_image_tags.get('<name>', 'latest')`,
so a pin that was never converted keeps rendering `:latest` and nothing says so — the
Deployment spec stops describing which bytes the pod runs, which is the whole point of the
content tag.

WHY THE SEED IS PART OF THE GUARD. `ansible/tests/_k8s_render.py` builds its context from role
defaults, group_vars and host_vars; a play fact reaches none of those. Left unseeded, every
`.get()` falls back to `'latest'`, every rendered-manifest guard in this directory keeps
passing, and not one of them reads the ref the cluster actually gets.
`scripts/lib/render_guard.py:BUILT_IMAGE_TAG_STUBS` is the stub map `BASE_CONTEXT` carries for
that reason, and the first test below holds its keys against the `image_builder_name` each
caller role declares — so an image added to the fleet without a seed entry fails here rather
than silently rendering `:latest` forever.

Run: uv run pytest ansible/tests/k8s/test_built_images_name_the_content_tag.py
"""

import re

from _helpers import ANSIBLE
from _k8s_render import rendered_docs
from lib.render_guard import BASE_CONTEXT, BUILT_IMAGE_TAG_STUBS

_POD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"})

# k8s_registry_pull_host is `localhost:<port>` — see test_built_images_pull_always.py.
_BUILT_PREFIX = "localhost:"

# `sha-` + 12 hex, the shape image-builder's content tag fact produces
# (ansible/tests/deploy/test_image_builder_content_tag.py pins it at the producer).
_CONTENT_TAG_RE = re.compile(r"^sha-[0-9a-f]{12}$")

_NAME_DECL_RE = re.compile(r"^\s*image_builder_name:\s*(\S+)\s*$", re.MULTILINE)

# The fleet this guard has to see. A census that finds its subjects by pattern returns an empty
# set the moment those files move, and an `all()` over nothing passes — so the members are named.
KNOWN_BUILT_IMAGES = frozenset(
    {
        "code-server",
        "homelab-mcp",
        "ical-proxy",
        "n8n",
        "n8n-runners",
        "nut",
        "pi-peer-backup",
        "terraria",
        "valheim",
    }
)


def built_image_names() -> set[str]:
    """Every image name a k8s role hands k8s/image-builder, read from the callers' tasks."""
    found: set[str] = set()
    for tasks in (ANSIBLE / "roles" / "k8s").glob("*/tasks/*.yml"):
        if tasks.parent.parent.name == "image-builder":
            continue
        found.update(_NAME_DECL_RE.findall(tasks.read_text()))
    return found


def _pod_spec(doc: dict) -> dict:
    spec = doc.get("spec", {})
    if doc.get("kind") == "CronJob":
        spec = spec.get("jobTemplate", {}).get("spec", {})
    return spec.get("template", {}).get("spec", {})


def built_image_refs(docs) -> list[tuple[str, str, str]]:
    """`(role, template, image ref)` for every container running an image-builder image."""
    names = built_image_names()
    found = []
    for role, tpl, doc in docs:
        if doc.get("kind") not in _POD_KINDS:
            continue
        pod = _pod_spec(doc)
        for key in ("initContainers", "containers"):
            for container in pod.get(key) or []:
                image = str(container.get("image", ""))
                if not image.startswith(_BUILT_PREFIX):
                    continue
                # `localhost:5000/registry-selftest:probe` is pushed by the registry role's own
                # selftest Job, not by image-builder, and has no content tag to name.
                if image.rsplit("/", 1)[-1].rsplit(":", 1)[0] not in names:
                    continue
                found.append((role, tpl, image))
    return found


def offenders(docs) -> list[str]:
    """`role/template: ref` for every built-image ref whose tag is not a content tag."""
    return [
        f"{role}/{tpl}: {image}"
        for role, tpl, image in built_image_refs(docs)
        if not _CONTENT_TAG_RE.match(image.rsplit(":", 1)[-1])
    ]


def builder_include_is_too_late(tasks: str) -> bool:
    """Does this role include k8s/manifests before k8s/image-builder?

    Pure over the task file's text so the red proof below can hand it a reversed copy. `False`
    for a role carrying only one of the two — n8n-images renders no manifest, and n8n's images
    are built by that sibling rather than by itself.
    """
    builder = tasks.find("name: k8s/image-builder")
    manifests = tasks.find("name: k8s/manifests")
    return builder >= 0 and manifests >= 0 and manifests < builder


def test_the_render_context_seeds_every_built_image():
    assert BASE_CONTEXT.get("k8s_built_image_tags") is BUILT_IMAGE_TAG_STUBS, (
        "BASE_CONTEXT no longer carries the stub map, so every render below falls back to "
        "`:latest` and this file's other guard checks a ref production never uses"
    )
    seeded = set(BUILT_IMAGE_TAG_STUBS)
    declared = built_image_names()
    assert declared >= KNOWN_BUILT_IMAGES, (
        "a role stopped declaring image_builder_name, or its tasks moved — the census this "
        f"guard runs on lost: {sorted(KNOWN_BUILT_IMAGES - declared)}"
    )
    assert seeded == declared, (
        "BUILT_IMAGE_TAG_STUBS must seed exactly the images k8s/image-builder builds. An "
        "unseeded image renders `:latest` under every guard here while production renders the "
        f"hash. Missing: {sorted(declared - seeded)}; stale: {sorted(seeded - declared)}"
    )
    bad = sorted(
        t for t in BUILT_IMAGE_TAG_STUBS.values() if not _CONTENT_TAG_RE.match(t)
    )
    assert not bad, f"seeded tags must have the shape image-builder produces: {bad}"


def test_every_built_image_pin_names_the_content_tag():
    refs = built_image_refs(rendered_docs())
    rendered = {image.rsplit("/", 1)[-1].rsplit(":", 1)[0] for _, _, image in refs}
    assert rendered >= KNOWN_BUILT_IMAGES, (
        "a built image stopped rendering into any manifest, so this guard no longer covers it: "
        f"{sorted(KNOWN_BUILT_IMAGES - rendered)}"
    )

    bad = offenders(rendered_docs())
    assert not bad, (
        "an in-cluster-built image is pinned to the mutable `:latest` alias. Read the content "
        "tag instead, so the Deployment spec names the bytes the pod runs: "
        "`{{ k8s_registry_pull_host }}/<name>:{{ k8s_built_image_tags.get('<name>', "
        "'latest') }}`. Offending pins: " + ", ".join(bad)
    )


def test_every_builder_include_precedes_the_manifests_include():
    """The pin resolves when the manifests render, so the fact has to exist by then.

    `k8s_built_image_tags` is a play fact, and a role that includes k8s/manifests first renders
    its Deployment before k8s/image-builder has published — the play dies on an undefined
    variable, or renders `:latest` if something else defined it. Seven of the nine images are
    built by the role that deploys them, so seven task files carry this ordering.
    """
    checked, late = [], []
    for role in sorted(KNOWN_BUILT_IMAGES):
        tasks = ANSIBLE / "roles" / "k8s" / role / "tasks" / "main.yml"
        if not tasks.is_file():
            continue
        body = tasks.read_text()
        if "name: k8s/image-builder" not in body:
            continue
        checked.append(role)
        if builder_include_is_too_late(body):
            late.append(role)
    assert len(checked) >= 6, (
        f"only {checked} include k8s/image-builder directly — the guard stopped seeing the "
        "roles it covers"
    )
    assert not late, (
        "these roles include k8s/manifests before k8s/image-builder, so their image pin reads "
        f"k8s_built_image_tags before it is published: {late}"
    )


def test_a_role_including_the_builder_after_its_manifests_is_flagged():
    assert builder_include_is_too_late(
        "- include_role:\n    name: k8s/manifests\n- include_role:\n    name: k8s/image-builder\n"
    )


def test_a_role_including_the_builder_first_is_clean():
    assert not builder_include_is_too_late(
        "- include_role:\n    name: k8s/image-builder\n- include_role:\n    name: k8s/manifests\n"
    )


def _doc(image: str) -> tuple[str, str, dict]:
    return (
        "r",
        "deployment.yaml.j2",
        {
            "kind": "Deployment",
            "spec": {
                "template": {"spec": {"containers": [{"name": "app", "image": image}]}}
            },
        },
    )


def test_a_built_image_naming_a_content_tag_is_clean():
    assert offenders([_doc("localhost:5000/nut:sha-0123456789ab")]) == []


def test_a_built_image_still_on_the_mutable_alias_is_flagged():
    assert offenders([_doc("localhost:5000/nut:latest")]) == [
        "r/deployment.yaml.j2: localhost:5000/nut:latest"
    ]


def test_a_registry_image_nothing_builds_is_out_of_scope():
    assert offenders([_doc("localhost:5000/registry-selftest:probe")]) == []


def test_an_upstream_image_is_out_of_scope():
    assert offenders([_doc("docker.io/library/alpine:3.24")]) == []
