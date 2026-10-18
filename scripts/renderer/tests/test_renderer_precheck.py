"""The role-owned kind that applies before `<tag>`: `templates/precheck/` into `<tag>-precheck` (#5719).

A role ships a pre-apply check, such as pihole's sibling guard, as `templates/precheck/`. The
renderer writes it to `cluster/<tag>-precheck/` and its own Kustomization, which `<tag>`
depends on, so a failing check holds the tag. The Job census admits a `precheck` Job there,
and `check-bundle` allows the Kustomization only for a role that ships the directory. The
ordering rules are in test_renderer_bundle_ordering.py.

Run: uv run pytest scripts/renderer/tests/test_renderer_precheck.py
"""

import shutil

from _job_census import HELPER, JOB_KIND, problems

from deploy_tools import bundle_census
from lib import yaml_fast
from lib.exit_codes import FAILED, OK, RENDER_OK
from lib.repo_paths import K8S_ROLES
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE
from renderer.renderer_lib.digest8 import configmap_digest8, job_spec_digest8
from renderer.renderer_lib.placeholders import render_placeholders

DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
TAG = "littlelink"
PRECHECK = f"{TAG}-precheck"
CHECK_TEMPLATE = (
    "kind: ConfigMap\nmetadata:\n  name: {{ manifests_service }}-precheck\n"
    "  namespace: default\n"
)


def _precheck_bundle(backoff: int) -> dict[str, list[dict]]:
    """`sonarr-precheck` holding a precheck Job, read-only, and the script ConfigMap it runs."""
    configmap = {
        "kind": "ConfigMap",
        "metadata": {"name": "sonarr-precheck-scripts"},
        "data": {
            "run.py": "import json\n\njson.loads('{}')\n",
            HELPER.name: HELPER.read_text(),
        },
    }
    configmap["metadata"]["name"] += f"-{configmap_digest8(configmap)}"
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "sonarr-precheck", "annotations": {JOB_KIND: "precheck"}},
        "spec": {
            "backoffLimit": backoff,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "run",
                            "image": "python",
                            "command": ["python3", "/scripts/run.py"],
                            "volumeMounts": [
                                {"name": "scripts", "mountPath": "/scripts"}
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "scripts",
                            "configMap": {"name": configmap["metadata"]["name"]},
                        }
                    ],
                }
            },
        },
    }
    job["metadata"]["name"] += f"-{job_spec_digest8(job)}"
    return {"sonarr-precheck": [job, configmap]}


def test_a_compliant_precheck_job_in_its_directory_is_clean():
    """`<tag>-precheck/` admits a `precheck` Job, and its `backoffLimit` is 0."""
    bundle = _precheck_bundle(0)
    assert [doc["kind"] for doc in bundle["sonarr-precheck"]] == ["Job", "ConfigMap"]
    assert problems(bundle) == []


def test_a_precheck_job_that_retries_is_flagged():
    rows = {line.split(":", 1)[0] for line in problems(_precheck_bundle(1))}
    assert rows == {"backoff"}


def test_a_rendered_precheck_directory_applies_before_its_tag_and_passes_check_bundle(
    tmp_path, capsys
):
    """The renderer writes `<tag>-precheck` from the role's input, and `<tag>` waits on it.

    The same bundle checked against the tree's littlelink, which ships no precheck manifest,
    fails, so the census allows the kind only for the role whose input calls for it.
    """
    roles = tmp_path / "roles"
    shutil.copytree(K8S_ROLES / TAG, roles / TAG)
    precheck = roles / TAG / "templates" / "precheck"
    precheck.mkdir()
    (precheck / "precheck-check.yaml.j2").write_text(CHECK_TEMPLATE)
    cluster_file = tmp_path / CLUSTER_VARS_FILE
    cluster_file.write_text(DOTENV)
    out = tmp_path / "out"
    code = homelab_render.cluster(
        out,
        [TAG],
        None,
        flux_owned=lambda: {TAG},
        cluster_file=cluster_file,
        render=lambda tags, keys: render_placeholders(tags, keys, roles=roles),
        vendored=roles,
    )
    assert code == RENDER_OK
    rendered = out / "cluster" / PRECHECK
    assert rendered.is_dir(), f"no cluster/{PRECHECK}/ was rendered"
    assert sorted(p.name for p in rendered.iterdir()) == [
        "kustomization.yaml",
        "precheck-check.yaml",
    ]
    assert f"name: {PRECHECK}" in (rendered / "precheck-check.yaml").read_text()
    docs = {
        d["metadata"]["name"]: d
        for d in yaml_fast.safe_load_all((out / "flux" / f"{TAG}.yaml").read_text())
    }
    assert set(docs) == {TAG, PRECHECK}
    assert {"name": PRECHECK} in docs[TAG]["spec"]["dependsOn"]
    assert TAG not in [e["name"] for e in docs[PRECHECK]["spec"].get("dependsOn", [])]

    assert bundle_census.report(out, [TAG], roles) == OK
    capsys.readouterr()
    assert bundle_census.report(out, [TAG]) == FAILED
