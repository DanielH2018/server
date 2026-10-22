"""Render netpol-baseline's verify Job for the tests that read it.

Shared by test_netpol_baseline_verify.py and test_netpol_baseline_flux_gates.py.
"""

from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs

TAG = "netpol-baseline"
VERIFY_DIR = K8S_ROLES / TAG / "templates" / "verify"


def write_stubs(tmp_path_factory) -> dict:
    """The stub-mode extra vars `shadow_diff.py stubs` writes, as the renderer's tests use."""
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def verify_render(stubs, overrides=None):
    """The tag's render under `overrides`; its `verify` holds the Job and its ConfigMap."""
    return cluster.render_tag(TAG, {**stubs, **(overrides or {})})


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
