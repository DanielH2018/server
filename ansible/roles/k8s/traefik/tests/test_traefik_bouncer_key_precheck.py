"""traefik's bouncer-key precheck: the LAPI request, the `traefik-precheck` render and its fence peer.

The script tests drive `check` with a stub opener. The render tests render a copy of the role
with `traefik_k8s_bouncer_key_precheck` switched on, as pihole's precheck tests do, and check
the Job meets every Job census row and sits in front of `traefik`. The default render holds no
Job, because the Job and its fence peer deadlock in one revision (#6095).

Run: uv run pytest ansible/roles/k8s/traefik/tests/test_traefik_bouncer_key_precheck.py
"""

import io
import re
import shutil
import urllib.error
from email.message import Message

import pytest

import traefik_bouncer_key_precheck as precheck
from _k8s_render import render_role_template
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from lib.repo_paths import ANSIBLE
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.bundle import bundle_files
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

TAG = "traefik"
PRECHECK = f"{TAG}-precheck"
SWITCH = "traefik_k8s_bouncer_key_precheck"
ROTATED_VAR = "crowdsec_k8s_bouncer_api_key"
ENV = {"LAPI_URL": "http://crowdsec:8080/", "BOUNCER_KEY": "k3y"}
DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"


class Answer:
    """A response `open_url` returns, as a context manager."""

    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(status: int, sent: list):
    def opener(request, timeout):
        sent.append(request)
        if status >= 400:
            raise urllib.error.HTTPError(
                request.full_url, status, "error", Message(), io.BytesIO(b"")
            )
        return Answer(status)

    return opener


def test_an_accepted_key_passes_and_is_sent_as_a_head():
    """A 200 passes, and the request is a HEAD, which adds no bouncer row and no `last_pull`."""
    sent = []
    assert precheck.main([], ENV, _opener(200, sent)) == 0
    (request,) = sent
    # A GET would make LAPI add a `k8straefik@<ip>` row and stamp its `last_pull`, which
    # bouncer_prune.py would read as the edge pulling.
    assert request.get_method() == "HEAD"
    assert request.full_url == "http://crowdsec:8080/v1/decisions?ip=127.0.0.1"
    assert request.get_header("X-api-key") == "k3y"


def test_a_rejected_key_fails_naming_the_bouncer_delete(capsys):
    """A 403 is the LAPI holding another key's hash, and the failure names the fix."""
    assert precheck.main([], ENV, _opener(403, [])) == 1
    assert "cscli bouncers delete k8straefik" in capsys.readouterr().err


def test_a_connect_with_no_answer_fails():
    """No answer past the helper's retries fails closed rather than passing."""

    def opener(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError())

    assert precheck.main([], ENV, opener) == 1


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _render_copy(tmp_path, stubs, switch: str = "true", key_digest: str | None = None):
    """The render of a copy of the role with the switch set, and the key's digest replaced."""
    tree = tmp_path / "ansible"
    role = tree / "roles" / "k8s" / TAG
    shutil.copytree(K8S_ROLES / TAG, role)
    # `crowdsec-secret.yaml.j2` embeds crowdsec's whitelists.
    shutil.copytree(
        K8S_ROLES / "crowdsec" / "files", role.parent / "crowdsec" / "files"
    )
    (tree / "vars").mkdir()
    source = (ANSIBLE / "vars" / "cluster-vars.source").read_text()
    if key_digest is not None:
        source, count = re.subn(
            rf"^{ROTATED_VAR} \S+$", f"{ROTATED_VAR} {key_digest}", source, flags=re.M
        )
        assert count == 1
    (tree / "vars" / "cluster-vars.source").write_text(source)
    defaults = role / "defaults" / "main.yml"
    text = defaults.read_text()
    assert f"{SWITCH}: false" in text
    defaults.write_text(text.replace(f"{SWITCH}: false", f"{SWITCH}: {switch}"))
    copied = {**stubs, "playbook_dir": str(tree)}
    return cluster.render_tag(TAG, copied, roles=tree / "roles" / "k8s").directories()


def _docs(files: dict[str, str]) -> list[dict]:
    return [
        doc
        for name, text in sorted(files.items())
        if name != cluster.KUSTOMIZATION
        for doc in yaml_fast.safe_load_all(text)
        if doc
    ]


def _job(files: dict[str, str]) -> dict:
    (job,) = [doc for doc in _docs(files) if doc["kind"] == "Job"]
    return job


def _seconds(duration: str) -> int:
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", duration)
    assert match, f"{duration!r} is not a duration of h, m and s"
    hours, minutes, secs = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(secs or 0)


@pytest.fixture(scope="module")
def switched_on(stubs, tmp_path_factory) -> dict[str, dict[str, str]]:
    return _render_copy(tmp_path_factory.mktemp("on"), stubs)


def test_the_switched_on_job_passes_every_job_census_row(switched_on):
    """In `traefik-precheck` the Job meets every row and reads the key from its own Secret."""
    files = switched_on[PRECHECK]
    job = _job(files)
    assert job["metadata"]["name"].startswith("traefik-precheck-")
    assert problems({PRECHECK: _docs(files)}) == []
    (secret,) = [doc for doc in _docs(files) if doc["kind"] == "Secret"]
    (container,) = job["spec"]["template"]["spec"]["containers"]
    (key,) = [e for e in container["env"] if e["name"] == "BOUNCER_KEY"]
    assert key["valueFrom"]["secretKeyRef"] == {
        "name": secret["metadata"]["name"],
        "key": "lapi_key",
    }


def test_traefik_waits_on_the_precheck_and_the_precheck_on_crowdsec(switched_on):
    """A failing precheck holds `traefik`, and it runs only once the LAPI is applied."""
    ordering = cluster.bundle_ordering(cluster.flux_owned_tags())
    files = bundle_files(switched_on, {}, [TAG], "300s", DOTENV, (), ordering)
    flux = {
        doc["metadata"]["name"]: doc
        for doc in yaml_fast.safe_load_all(files[f"flux/{TAG}.yaml"])
    }
    assert {"name": PRECHECK} in flux[TAG]["spec"]["dependsOn"]
    assert {"name": "crowdsec"} in flux[PRECHECK]["spec"]["dependsOn"]
    deadline = _job(switched_on[PRECHECK])["spec"]["activeDeadlineSeconds"]
    assert _seconds(flux[PRECHECK]["spec"]["timeout"]) > deadline


def test_a_key_rotation_renames_the_job(switched_on, stubs, tmp_path):
    """A Job that kept its name would satisfy `dependsOn` with the old key's pass."""
    rotated = _render_copy(tmp_path, stubs, key_digest="0" * 64)
    before = _job(switched_on[PRECHECK])["metadata"]["name"]
    assert _job(rotated[PRECHECK])["metadata"]["name"] != before


def test_the_default_renders_no_job_and_leaves_the_service_account(stubs, tmp_path):
    """Off, `traefik-precheck` holds the ServiceAccount alone, so the flip adds only the Job."""
    directories = _render_copy(tmp_path, stubs, "false")
    assert [doc["kind"] for doc in _docs(directories[PRECHECK])] == ["ServiceAccount"]


def test_the_lapi_fence_admits_the_precheck_on_the_lapi_port_alone():
    """The peer lands with the Job off; it must not share traefik's AppSec port."""
    (policy,) = yaml_fast.safe_load_all(
        render_role_template("netpol-baseline", "networkpolicy-crowdsec.yaml.j2")
    )
    rules = [
        rule
        for rule in policy["spec"]["ingress"]
        if {"podSelector": {"matchLabels": {"app": PRECHECK}}} in rule.get("from", [])
    ]
    assert len(rules) == 1
    assert [port["port"] for port in rules[0]["ports"]] == [8080]
