"""Render netpol-baseline's inert verify Job as its flip PR will emit it.

The helpers copy the role, move `templates/verify-inert/` to `templates/verify/`, and render
the copy, so a test reads the Job Flux will receive while the tree's own render emits no
`netpol-baseline-verify`. Shared by test_netpol_baseline_verify.py and
test_netpol_baseline_flux_gates.py.
"""

import shutil

from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs

TAG = "netpol-baseline"
STAGED = K8S_ROLES / TAG / "templates" / "verify-inert"


def write_stubs(tmp_path_factory) -> dict:
    """The stub-mode extra vars `shadow_diff.py stubs` writes, as the renderer's tests use."""
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def flipped_render(stubs, roles, overrides=None):
    shutil.copytree(K8S_ROLES / TAG, roles / TAG)
    templates = roles / TAG / "templates"
    (templates / "verify-inert").rename(templates / "verify")
    return cluster.render_tag(TAG, {**stubs, **(overrides or {})}, roles=roles)


def docs(rendered) -> list[dict]:
    return [
        doc
        for name, text in sorted(rendered.verify.items())
        if name != "kustomization.yaml"
        for doc in yaml_fast.safe_load_all(text)
        if doc
    ]


def job(rendered_docs):
    return next(doc for doc in rendered_docs if doc["kind"] == "Job")


def env_of(container) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in container["env"]}
