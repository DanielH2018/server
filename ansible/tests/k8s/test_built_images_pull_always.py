#!/usr/bin/env python3
"""Every container running an in-cluster-built image must declare `imagePullPolicy: Always`.

WHY THIS EXISTS. image-builder pushes every build to the same mutable tag
(`localhost:5000/<name>:latest`), so a rebuild changes what the registry serves without
changing the Deployment spec. The rollout that k8s/manifests queues then creates a pod that
asks the node for `:latest`, and under `IfNotPresent` the node answers from its cache: the
pod rolls, reads Ready, and runs the OLD bytes. Only the drift gate in
post_tasks/k8s_image_drift_gate.yml notices, and it notices after the rollout, as a failed
deploy.

WHY EXPLICIT rather than trusting the `:latest` default. The API server defaults this field
only when it is absent at CREATE time. terraria's Deployment was created on 2026-08-31 with an
upstream `@sha256:` pin, which defaults to `IfNotPresent`; the switch to the built `:latest`
changed only `image`, and a client-side apply leaves a defaulted field it never managed in
place. The first Renovate digest bump of that image (PR #681, 2026-09-02) rebuilt it, rolled
the pod onto the cached copy, and failed the drift gate twice — once more with
`image_builder_force=true`, which rebuilds but cannot make the node re-pull. Of the seven
image-builder consumers only homelab-mcp and n8n wrote the line; the other four ran `Always`
because their Deployments happened to be CREATED with a `:latest` image, which is one
`kubectl apply` of a digest-pinned draft away from terraria's state. This pins the class.

Run: uv run pytest ansible/tests/k8s/test_built_images_pull_always.py
"""

from _k8s_render import rendered_docs

_POD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"})

# k8s_registry_pull_host is `localhost:<port>`; nothing else on the plane pulls from a
# loopback registry, so the prefix is the whole test of "built here".
_BUILT_PREFIX = "localhost:"


def _pod_spec(doc: dict) -> dict:
    spec = doc.get("spec", {})
    if doc.get("kind") == "CronJob":
        spec = spec.get("jobTemplate", {}).get("spec", {})
    return spec.get("template", {}).get("spec", {})


def offenders(docs) -> list[str]:
    """`role/template:container` for every built-image container not pulling Always."""
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
                if container.get("imagePullPolicy") != "Always":
                    found.append(f"{role}/{tpl}:{container.get('name', '<unnamed>')}")
    return found


def test_every_built_image_container_pulls_always():
    built = [
        (role, tpl)
        for role, tpl, doc in rendered_docs()
        if doc.get("kind") in _POD_KINDS
        and any(
            str(c.get("image", "")).startswith(_BUILT_PREFIX)
            for key in ("initContainers", "containers")
            for c in _pod_spec(doc).get(key) or []
        )
    ]
    # The collector has to see the fleet it guards: seven roles include k8s/image-builder.
    assert len(built) >= 6, f"only {len(built)} built-image workloads rendered: {built}"

    bad = offenders(rendered_docs())
    assert not bad, (
        "in-cluster-built images are pushed to a mutable :latest, so a pod created without "
        "imagePullPolicy: Always runs whatever the node cached — the rollout reads green and "
        "the drift gate fails the deploy afterwards. Add the line next to `image:` in: "
        + ", ".join(bad)
    )


def _doc(image: str, policy: str | None) -> tuple[str, str, dict]:
    container = {"name": "app", "image": image}
    if policy:
        container["imagePullPolicy"] = policy
    return (
        "r",
        "deployment.yaml.j2",
        {
            "kind": "Deployment",
            "spec": {"template": {"spec": {"containers": [container]}}},
        },
    )


def test_a_built_image_pulling_always_is_clean():
    assert offenders([_doc("localhost:5000/x:latest", "Always")]) == []


def test_a_built_image_without_the_policy_is_flagged():
    assert offenders([_doc("localhost:5000/x:latest", None)]) == [
        "r/deployment.yaml.j2:app"
    ]


def test_a_built_image_pulling_if_not_present_is_flagged():
    assert offenders([_doc("localhost:5000/x:latest", "IfNotPresent")]) == [
        "r/deployment.yaml.j2:app"
    ]


def test_an_upstream_image_is_out_of_scope():
    assert offenders([_doc("docker.io/library/alpine:3.24@sha256:abc", None)]) == []
