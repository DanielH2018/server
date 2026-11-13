"""system-upgrade's verify Jobs: the script fails a node whose k3s binary differs from the pin.

The script tests run `system_upgrade_verify.main` with a stand-in for the Node read and a
fixture binary. The render tests render the tree through `render_tag`, so the Jobs are checked
as Flux receives them in `system-upgrade-verify`, and render edited copies of the role for the
empty-list and pin-bump cases. ADR-0025 records the decision.

Run: uv run pytest k8s/roles/system-upgrade/tests/test_system_upgrade_verify.py
"""

import hashlib
import re
import shutil
from pathlib import Path

import pytest

import system_upgrade_verify
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

TAG = "system-upgrade"
# A path into a role's tree, as any lookup names it; group 1 is the role.
ROLE_PATH = re.compile(r"\bk8s/roles/([^/'\"]+)/")
DEFAULTS = yaml_fast.safe_load((K8S_ROLES / TAG / "defaults/main.yml").read_text())
BINARY = b"fixture k3s binary"
SUM = hashlib.sha256(BINARY).hexdigest()
VERSION = "v1.37.2+k3s1"


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    binary = tmp_path / "k3s"
    binary.write_bytes(BINARY)
    return {
        "NODE_NAME": "daniel-server",
        "K3S_VERSION": VERSION,
        "K3S_SHA256": SUM,
        "K3S_BINARY": str(binary),
    }


def _reader(kubelet: str | None):
    def read(name: str) -> dict:
        assert name == "daniel-server"
        info = {} if kubelet is None else {"kubeletVersion": kubelet}
        return {"status": {"nodeInfo": info}}

    return read


def test_a_binary_matching_the_pin_passes(env, capsys):
    assert system_upgrade_verify.main([], env, _reader(VERSION)) == 0
    assert f"matches {SUM}" in capsys.readouterr().out


def test_a_binary_at_the_pin_with_another_sum_fails_and_names_the_node(env, capsys):
    env["K3S_SHA256"] = "0" * 64
    assert system_upgrade_verify.main([], env, _reader(VERSION)) == 1
    err = capsys.readouterr().err
    assert f"node daniel-server: the k3s binary hashes to {SUM}" in err
    assert "0" * 64 in err


def test_a_node_on_another_version_reads_pending(env, capsys):
    """A hold can leave a node on the old version while the pin moved."""
    env["K3S_SHA256"] = "0" * 64
    assert system_upgrade_verify.main([], env, _reader("v1.36.4+k3s1")) == 0
    assert "pending" in capsys.readouterr().out


def test_the_munged_form_of_the_pin_counts_as_the_pin(env):
    env["K3S_SHA256"] = "0" * 64
    assert system_upgrade_verify.main([], env, _reader("v1.37.2-k3s1")) == 1


@pytest.mark.parametrize("kubelet", [None, ""], ids=["absent", "empty"])
def test_a_node_reporting_no_version_fails(env, kubelet):
    assert system_upgrade_verify.main([], env, _reader(kubelet)) == 1


def test_an_unreadable_binary_fails(env, tmp_path, capsys):
    env["K3S_BINARY"] = str(tmp_path / "missing")
    assert system_upgrade_verify.main([], env, _reader(VERSION)) == 1
    assert "cannot read the k3s binary" in capsys.readouterr().err


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _render_copy(roles: Path, stubs, edit=None) -> list[dict]:
    """The verify directory of a copy of the role, its defaults edited by `edit` when given."""
    shutil.copytree(K8S_ROLES / TAG, roles / TAG)
    defaults = roles / TAG / "defaults/main.yml"
    if edit:
        defaults.write_text(edit(defaults.read_text()))
    return _docs(cluster.render_tag(TAG, stubs, roles=roles).verify)


def _docs(texts: dict[str, str]) -> list[dict]:
    return [
        doc
        for name, text in sorted(texts.items())
        if name != "kustomization.yaml"
        for doc in yaml_fast.safe_load_all(text)
        if doc
    ]


def _jobs(docs: list[dict]) -> list[dict]:
    return [doc for doc in docs if doc["kind"] == "Job"]


def _env_of(job: dict) -> dict[str, str]:
    (container,) = job["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e["value"] for e in container["env"]}


@pytest.fixture(scope="module")
def rendered(stubs) -> list[dict]:
    return _docs(cluster.render_tag(TAG, stubs).verify)


def test_the_render_passes_every_job_census_row(rendered):
    assert len(_jobs(rendered)) >= 2
    assert problems({f"{TAG}-verify": rendered}) == []


def test_the_census_fails_a_job_whose_spec_moved_under_its_name(rendered):
    """The red case for the name row: an edited spec under the rendered name is refused."""
    docs = [dict(doc) for doc in rendered]
    job = _jobs(docs)[0]
    job["spec"] = {**job["spec"], "backoffLimit": 1}
    found = problems({f"{TAG}-verify": docs})
    assert any(line.startswith("name: ") for line in found)


def test_each_selected_node_gets_one_job_pinned_to_it(rendered):
    nodes = DEFAULTS["system_upgrade_nodes"]
    assert {"daniel-box", "daniel-server"} <= set(nodes)
    jobs = _jobs(rendered)
    assert sorted(
        job["spec"]["template"]["spec"]["nodeSelector"]["kubernetes.io/hostname"]
        for job in jobs
    ) == sorted(nodes)
    for job in jobs:
        env = _env_of(job)
        pod = job["spec"]["template"]["spec"]
        assert env["NODE_NAME"] == pod["nodeSelector"]["kubernetes.io/hostname"]
        assert (env["K3S_VERSION"], env["K3S_SHA256"]) == (
            DEFAULTS["k3s_version"],
            DEFAULTS["k3s_binary_sha256_amd64"],
        )
        assert pod["serviceAccountName"] == "k3s-plan"


def test_the_job_mounts_only_the_k3s_binary_from_the_host_read_only(rendered):
    assert _jobs(rendered)
    for job in _jobs(rendered):
        pod = job["spec"]["template"]["spec"]
        host = [v for v in pod["volumes"] if "hostPath" in v]
        assert [v["hostPath"] for v in host] == [
            {"path": "/usr/local/bin/k3s", "type": "File"}
        ]
        (container,) = pod["containers"]
        (mount,) = [
            m for m in container["volumeMounts"] if m["name"] == host[0]["name"]
        ]
        assert mount["readOnly"] is True
        assert mount["mountPath"] == _env_of(job)["K3S_BINARY"]


def test_an_empty_node_list_renders_no_job(stubs, tmp_path):
    docs = _render_copy(
        tmp_path,
        stubs,
        lambda text: re.sub(
            r"^system_upgrade_nodes: .*$", "system_upgrade_nodes: []", text, flags=re.M
        ),
    )
    assert [doc["kind"] for doc in docs] == ["ConfigMap"]
    assert problems({f"{TAG}-verify": docs}) == []


def test_a_sum_bump_renames_every_job(stubs, tmp_path):
    """A new pin is a new spec, so Flux runs the Jobs again after the Plans finish."""
    sum_ = DEFAULTS["k3s_binary_sha256_amd64"]
    names = [
        sorted(
            job["metadata"]["name"]
            for job in _jobs(
                _render_copy(
                    tmp_path / str(n),
                    stubs,
                    edit and (lambda text: text.replace(sum_, "f" * 64)),
                )
            )
        )
        for n, edit in enumerate((False, True))
    ]
    assert len(names[0]) == len(names[1]) >= 2
    assert not set(names[0]) & set(names[1])


def test_the_job_runs_the_flux_job_image(rendered):
    flux = yaml_fast.safe_load((K8S_ROLES / "flux/defaults/main.yml").read_text())
    assert _jobs(rendered)
    for job in _jobs(rendered):
        pod = job["spec"]["template"]["spec"]
        (container,) = pod["containers"]
        assert container["image"] == flux["flux_job_image"]
        assert "@sha256:" in container["image"]
        assert pod["securityContext"]["runAsNonRoot"] is True


def test_the_verify_job_reads_no_other_roles_file():
    """The helper and the image come from `render_tag`, never a lookup into another role."""
    staged = sorted((K8S_ROLES / TAG / "templates" / "verify").glob("*.j2"))
    assert [p.name for p in staged] == ["job.yaml.j2"]
    assert [
        (path.name, role)
        for path in staged
        for role in ROLE_PATH.findall(path.read_text())
        if role != TAG
    ] == []
