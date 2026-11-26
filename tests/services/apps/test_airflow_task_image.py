"""Airflow's task pods run the scheduler's image, at the GHCR ref the renderer rewrites it to.

The pod template is ConfigMap text, which `images.py:ghcr_text` never rewrites, so the role
writes the GHCR ref itself (`k8s/roles/airflow/defaults/main.yml:airflow_k8s_task_image`).
"""

from _k8s_render import rendered_docs

from lib.estate import shared_vars
from lib import yaml_fast
from renderer.renderer_lib.images import GHCR


def _airflow_docs():
    return [doc for role, _, doc in rendered_docs() if role == "airflow"]


def test_the_task_pod_template_names_the_schedulers_image_at_ghcr():
    # fact: k8s/roles/airflow/CLAUDE.md#At a glance
    docs = _airflow_docs()
    scheduler = next(
        d
        for d in docs
        if d.get("kind") == "Deployment"
        and d["metadata"]["name"] == "airflow-scheduler"
    )
    template = next(
        d
        for d in docs
        if d.get("kind") == "ConfigMap"
        and d["metadata"]["name"] == "airflow-pod-template"
    )
    scheduler_image = scheduler["spec"]["template"]["spec"]["containers"][0]["image"]
    pod = yaml_fast.safe_load(template["data"]["pod-template.yaml"])
    (base,) = pod["spec"]["containers"]

    name_and_tag = scheduler_image.split("/", 1)[1]
    assert name_and_tag.startswith("airflow:sha-")
    assert base["image"] == f"{GHCR}/{name_and_tag}"


def test_the_ghcr_pull_host_variable_is_the_renderers_ghcr_host():
    group_vars = shared_vars()

    assert group_vars["k8s_ghcr_pull_host"] == GHCR
