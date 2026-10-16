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
from _job_census import HELPER, JOB_KIND, RERUN_SAFE, load, problems, script_offence

from deploy_tools.bundle_census import published_tags
from lib.exit_codes import RENDER_OK
from renderer import homelab_render
from renderer.renderer_lib.bundle import (
    CLUSTER_VARS_FILE,
    SHARED_KINDS,
    configmap_digest8,
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
    "import json\nimport re\n\nfrom flux_job_connect import Request, open_url\n\n"
    "request = Request('http://sonarr.media.svc:8989/ping')\n"
    "with open_url(request, timeout=10) as r:\n"
    "    json.loads(r.read())\n"
    "re.compile('ok')\n"
)
RUN = ["python3", "/scripts/run.py"]


def _named(job: dict) -> dict:
    job["metadata"]["name"] = f"{job['metadata']['name']}-{job_spec_digest8(job)}"
    return job


def _configmap(script: str = THROUGH_HELPER, helper: str | None = None) -> dict:
    """The Job's script ConfigMap, named by its digest; `helper=""` leaves the copy out."""
    data = {"run.py": script}
    if helper != "":
        data[HELPER.name] = helper or HELPER.read_text()
    configmap = {"kind": "ConfigMap", "metadata": {"name": SCRIPTS}, "data": data}
    configmap["metadata"]["name"] = f"{SCRIPTS}-{configmap_digest8(configmap)}"
    return configmap


def _job(kind: str, configmap: dict | None = None, command=RUN) -> dict:
    """A Job of `kind` that runs `configmap`'s `run.py` and passes every row."""
    configmap = configmap or _configmap()
    annotations = {JOB_KIND: kind}
    if kind == "mutation":
        annotations[RERUN_SAFE] = THIS_TEST
    container = {
        "name": "run",
        "image": "python",
        "command": command,
        "volumeMounts": [{"name": "scripts", "mountPath": "/scripts"}],
    }
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
                        "containers": [container],
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
    )


def _bundle(job: dict, directory: str, configmap: dict | None = None) -> dict:
    return {directory: [job, configmap or _configmap()]}


def _verify(**configmap) -> dict:
    """A verify Job's bundle whose script ConfigMap is `_configmap(**configmap)`."""
    built = _configmap(**configmap)
    return _bundle(_job("verify", built), "sonarr-verify", built)


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
        "import urllib.request\n\nurllib.request.urlretrieve('http://sonarr:8989/')\n",
        "import http.client\n\nhttp.client.HTTPConnection('sonarr', 8989).request('GET', '/')\n",
        "import socket\n\nsocket.create_connection(('sonarr', 8989))\n",
        "import asyncio\n\nasyncio.run(asyncio.open_connection('sonarr', 8989))\n",
        "import importlib\n\nimportlib.import_module('socket')\n",
        "import ftplib\n\nftplib.FTP('sonarr')\n",
        "import os\n\nos.system('curl http://sonarr:8989/ping')\n",
        "import flux_job_connect\n\nflux_job_connect.urllib.request.urlopen('http://x')\n",
        "exec('import socket')\n",
    ],
)
def test_a_script_that_connects_without_the_helper_is_flagged(script):
    assert _rows(_verify(script=script)) == {"helper"}


# #5274: each reaches a connecting module through one ALLOWED_IMPORTS lets in, by a name
# FORBIDDEN does not hold, so the import allowlist alone does not catch it.
@pytest.mark.parametrize(
    "script",
    [
        pytest.param(
            "import flux_job_connect as f\n\n"
            "f.urllib.request.HTTPHandler().http_open(f.Request('http://x'))\n",
            id="handler-through-helper",
        ),
        pytest.param(
            "import urllib.error\n\nimport flux_job_connect\n\n"
            "urllib.request.HTTPHandler().http_open(flux_job_connect.Request('http://x'))\n",
            id="handler-through-urllib-error",
        ),
        pytest.param(
            "import sys, flux_job_connect\n\n"
            "sys.modules['socket'].socket().connect(('x', 80))\n",
            id="sys-modules-socket",
        ),
    ],
)
def test_script_offence_flags_a_connection_through_an_allowed_module(script):
    assert script_offence("run.py", script)


@pytest.mark.parametrize(
    "script",
    [
        pytest.param(
            "import flux_job_connect as f\n\n"
            "f.urllib.request.HTTPSHandler().https_open(f.Request('https://x'))\n",
            id="https-handler",
        ),
        pytest.param(
            "import flux_job_connect as f\n\n"
            "f.urllib.request.socket.socket().connect(('x', 80))\n",
            id="socket-attribute",
        ),
    ],
)
def test_a_job_whose_script_reaches_a_module_through_the_helper_is_flagged(script):
    assert _rows(_verify(script=script)) == {"helper"}


@pytest.mark.parametrize(
    "script",
    [
        pytest.param(
            "import urllib.error\n\n"
            "urllib.error.urllib.request.HTTPHandler().http_open(None)\n",
            id="package-through-urllib-error",
        ),
        pytest.param(
            "from flux_job_connect import urllib\n\nurllib.request.HTTPHandler()\n",
            id="from-helper-import-module",
        ),
        pytest.param(
            "from sys import modules\n\nmodules['socket']\n",
            id="from-sys-import-modules",
        ),
    ],
)
def test_script_offence_flags_a_module_reached_by_another_route(script):
    assert script_offence("run.py", script)


def test_script_offence_passes_the_helper_exports_and_admitted_urllib_submodules():
    script = (
        "import urllib.error\nimport urllib.parse\n\nimport flux_job_connect as f\n\n"
        "try:\n"
        "    f.open_url(f.Request(urllib.parse.quote('http://x')), timeout=1)\n"
        "except urllib.error.HTTPError as err:\n"
        "    f.refused(err)\n"
    )
    assert script_offence("run.py", script) == []


def test_a_changed_copy_of_the_helper_is_flagged():
    helper = HELPER.read_text().replace("RETRY_SECONDS = 60", "RETRY_SECONDS = 0")
    assert _rows(_verify(helper=helper)) == {"helper"}


def test_a_script_importing_the_helper_without_its_copy_is_flagged():
    assert _rows(_verify(helper="")) == {"helper"}


@pytest.mark.parametrize(
    "command",
    [
        ["python3", "-c", "import urllib.request"],
        ["python3", "-m", "run"],
        ["sh", "/scripts/run.sh"],
        ["python3", "/scripts/run"],
        None,
    ],
)
def test_a_container_that_runs_no_mounted_script_is_flagged(command):
    assert _rows(_bundle(_job("verify", command=command), "sonarr-verify")) == {
        "delivery"
    }


def test_a_script_path_outside_the_mounted_configmap_is_flagged():
    job = _job("verify", command=["python3", "/scripts/other.py"])
    assert _rows(_bundle(job, "sonarr-verify")) == {"delivery"}


def test_a_script_configmap_named_without_its_digest_is_flagged():
    configmap = _configmap()
    configmap["metadata"]["name"] = SCRIPTS
    bundle = _bundle(_job("verify", configmap), "sonarr-verify", configmap)
    assert _rows(bundle) == {"name"}


def test_a_script_fix_renames_the_job():
    fixed = _configmap(script=THROUGH_HELPER + "print('fixed')\n")
    assert (
        _job("verify")["metadata"]["name"] != _job("verify", fixed)["metadata"]["name"]
    )


def test_a_placeholder_in_a_job_spec_is_flagged():
    job = _broken("verify", lambda j: j["spec"]["template"].update(env="${domain}"))
    assert _rows(_bundle(job, "sonarr-verify")) == {"name"}


def test_an_escaped_placeholder_in_a_job_spec_is_clean():
    job = _broken("verify", lambda j: j["spec"]["template"].update(env="$${HOME}"))
    assert problems(_bundle(job, "sonarr-verify")) == []


def test_a_mounted_configmap_the_directory_lacks_is_flagged():
    assert _rows({"sonarr-verify": [_job("verify")]}) == {"delivery", "helper"}


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
