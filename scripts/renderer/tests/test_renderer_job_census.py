"""D5.1a3's Job census, over fixture Jobs of each kind and over the live publish bundle.

Each red fixture is a compliant Job with one rule broken, and the test asserts that exactly
that row fires, so a row that silently stopped matching fails its own pair. The live bundle
renders every published tag, the way the render job does, with a fixture cluster file. It
holds no Job until D5.2a renders the first snapshot Job, so the live test also asserts which
directories the census read: a walk that reached nothing would otherwise pass.

Run: uv run pytest scripts/renderer/tests/test_renderer_job_census.py
"""

import copy

import pytest
from _job_census import HELPER, JOB_KIND, RERUN_SAFE, load, problems

from deploy_tools.bundle_census import published_tags
from lib.exit_codes import RENDER_OK
from renderer import homelab_render
from renderer.renderer_lib.bundle import (
    CLUSTER_VARS_FILE,
    SHARED_KINDS,
    job_spec_digest8,
)

DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
SCRIPTS = "sonarr-seed-scripts"
THIS_TEST = (
    "scripts/renderer/tests/test_renderer_job_census.py"
    "::test_a_compliant_job_of_each_kind_is_clean"
)
DIRECTORY = {
    "snapshot": "sonarr-snapshot",
    "soak": "soak",
    "verify": "sonarr-verify",
    "mutation": "sonarr-verify",
}
BACKOFF = {"snapshot": 1, "soak": 0, "verify": 0, "mutation": 2}
THROUGH_HELPER = (
    "from flux_job_connect import open_url\n\n"
    "with open_url('http://sonarr.media.svc:8989/ping', timeout=10) as r:\n"
    "    r.read()\n"
)


def _named(job: dict) -> dict:
    job["metadata"]["name"] = f"{job['metadata']['name']}-{job_spec_digest8(job)}"
    return job


def _job(kind: str) -> dict:
    """A Job of `kind` that passes every row."""
    annotations = {JOB_KIND: kind}
    if kind == "mutation":
        annotations[RERUN_SAFE] = THIS_TEST
    return _named(
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": f"sonarr-{kind}", "annotations": annotations},
            "spec": {
                "backoffLimit": BACKOFF[kind],
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "volumes": [
                            {"name": "scripts", "configMap": {"name": SCRIPTS}}
                        ],
                    }
                },
            },
        }
    )


def _configmap(script: str, helper: str | None = None) -> dict:
    """The Job's script ConfigMap, with `helper` as its helper copy; "" leaves the copy out."""
    data = {"run.py": script}
    if helper != "":
        data[HELPER.name] = helper or HELPER.read_text()
    return {"kind": "ConfigMap", "metadata": {"name": SCRIPTS}, "data": data}


def _bundle(job: dict, directory: str, script: str = THROUGH_HELPER, **kw) -> dict:
    return {directory: [job, _configmap(script, **kw)]}


def _rows(bundle: dict) -> set[str]:
    """The rows the bundle fails."""
    return {line.split(":", 1)[0] for line in problems(bundle)}


def _broken(kind: str, change) -> dict:
    """A compliant Job of `kind` with `change` applied, then renamed to its new digest."""
    job = copy.deepcopy(_job(kind))
    job["metadata"]["name"] = f"sonarr-{kind}"
    change(job)
    return _named(job)


@pytest.mark.parametrize("kind", sorted(DIRECTORY))
def test_a_compliant_job_of_each_kind_is_clean(kind):
    assert problems(_bundle(_job(kind), DIRECTORY[kind])) == []


@pytest.mark.parametrize("kind", sorted(DIRECTORY))
def test_a_job_of_each_kind_with_a_ttl_is_flagged(kind):
    job = _broken(kind, lambda j: j["spec"].update(ttlSecondsAfterFinished=86400))
    assert _rows(_bundle(job, DIRECTORY[kind])) == {"ttl"}


def test_a_mutation_job_without_rerun_safe_is_flagged():
    job = _broken("mutation", lambda j: j["metadata"]["annotations"].pop(RERUN_SAFE))
    assert _rows(_bundle(job, "sonarr-verify")) == {"rerun-safe"}


def test_a_rerun_safe_annotation_naming_no_test_is_flagged():
    job = _job("mutation")
    job["metadata"]["annotations"][RERUN_SAFE] = THIS_TEST + "_gone"
    assert _rows(_bundle(job, "sonarr-verify")) == {"rerun-safe"}


def test_a_name_without_its_spec_digest_is_flagged():
    job = _job("verify")
    job["spec"]["template"]["spec"]["activeDeadlineSeconds"] = 300
    assert _rows(_bundle(job, "sonarr-verify")) == {"name"}


@pytest.mark.parametrize("kind", ["snapshot", "mutation"])
def test_a_job_without_a_backoff_limit_is_flagged(kind):
    job = _broken(kind, lambda j: j["spec"].pop("backoffLimit"))
    assert _rows(_bundle(job, DIRECTORY[kind])) == {"backoff"}


def test_a_verify_job_that_retries_is_flagged():
    job = _broken("verify", lambda j: j["spec"].update(backoffLimit=1))
    assert _rows(_bundle(job, "sonarr-verify")) == {"backoff"}


def test_a_job_with_no_kind_annotation_is_flagged():
    job = _job("verify")
    job["metadata"]["annotations"].pop(JOB_KIND)
    assert _rows(_bundle(job, "sonarr-verify")) == {"kind"}


@pytest.mark.parametrize("directory", ["sonarr", "sonarr-deferred", "cluster-vars"])
def test_a_job_outside_a_job_directory_is_flagged(directory):
    assert _rows(_bundle(_job("verify"), directory)) == {"kind"}


@pytest.mark.parametrize(
    "script",
    [
        "import urllib.request\n\nurllib.request.urlopen('http://sonarr:8989/ping')\n",
        "from urllib.request import urlopen\n\nurlopen('http://sonarr:8989/ping')\n",
        "import http.client\n\nhttp.client.HTTPConnection('sonarr', 8989).request('GET', '/')\n",
        "import socket\n\nsocket.create_connection(('sonarr', 8989))\n",
    ],
)
def test_a_script_that_connects_without_the_helper_is_flagged(script):
    assert _rows(_bundle(_job("verify"), "sonarr-verify", script)) == {"helper"}


def test_a_changed_copy_of_the_helper_is_flagged():
    helper = HELPER.read_text().replace("RETRY_SECONDS = 60", "RETRY_SECONDS = 0")
    bundle = _bundle(_job("verify"), "sonarr-verify", helper=helper)
    assert _rows(bundle) == {"helper"}


def test_a_script_importing_the_helper_without_its_copy_is_flagged():
    bundle = _bundle(_job("verify"), "sonarr-verify", helper="")
    assert _rows(bundle) == {"helper"}


def test_a_placeholder_in_a_job_spec_is_flagged():
    job = _broken("verify", lambda j: j["spec"]["template"].update(env="${domain}"))
    assert _rows(_bundle(job, "sonarr-verify")) == {"name"}


def test_an_escaped_placeholder_in_a_job_spec_is_clean():
    job = _broken("verify", lambda j: j["spec"]["template"].update(env="$${HOME}"))
    assert problems(_bundle(job, "sonarr-verify")) == []


def test_a_mounted_configmap_the_directory_lacks_is_flagged():
    assert _rows({"sonarr-verify": [_job("verify")]}) == {"helper"}


@pytest.fixture
def live_bundle(tmp_path):
    """The publish bundle of every published tag and shared kind, as the render job writes it."""
    cluster_file = tmp_path / CLUSTER_VARS_FILE
    cluster_file.write_text(DOTENV)
    tags = published_tags()
    out = tmp_path / "out"
    code = homelab_render.cluster(
        out, tags, None, flux_owned=lambda: set(tags), cluster_file=cluster_file
    )
    assert code == RENDER_OK
    return tags, load(out)


def test_the_live_bundle_passes_every_row(live_bundle):
    tags, bundle = live_bundle
    assert problems(bundle) == []
    assert set(tags) | SHARED_KINDS <= bundle.keys()
    assert {"bento-pdf", "cluster-vars"} <= bundle.keys()
    assert all(bundle[tag] for tag in tags), "a tag directory parsed to no documents"
