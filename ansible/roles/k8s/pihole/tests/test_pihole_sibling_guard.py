"""pihole's sibling guard: the Job that holds instance 1's Flux roll while pihole-2 serves nothing.

The script tests run `pihole_sibling_guard.main` against `Slices`, a fixture API server that
answers each EndpointSlice list from a queue per Service, so a test can script pihole-2 coming
back mid-wait. The render tests copy the role and move `templates/guard-inert/` into
`templates/verify/`, the one role-owned directory the renderer reads, so the guard is checked
as the renderer emits it. That placement is only the render vehicle: under Flux the guard must
run before `pihole`, in a kind the renderer does not have yet (#5719). Its RBAC is tested
against the script's calls in ansible/tests/k8s/test_rbac_against_script.py.

Run: uv run pytest ansible/roles/k8s/pihole/tests/test_pihole_sibling_guard.py
"""

import shutil
from collections.abc import Mapping

import pytest

import pihole_sibling_guard
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from lib.repo_paths import ANSIBLE
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import ROWS, problems

TAG = "pihole"
NAMESPACE = "homelab"
ENV = {
    "GUARD_NAMESPACE": NAMESPACE,
    "GUARD_INSTANCE_SERVICE": "pihole",
    "GUARD_SIBLING_SERVICE": "pihole-2-web",
    "GUARD_WAIT_SECONDS": "300",
    "PIHOLE_ROLL_DIGEST8": "abcd1234",
}
READY = {"addresses": ["10.42.0.7"], "conditions": {"ready": True}}
NOT_READY = {"addresses": ["10.42.0.7"], "conditions": {"ready": False}}
# Inside its grace period: the API may still report it ready.
TERMINATING = {
    "addresses": ["10.42.0.7"],
    "conditions": {"ready": True, "terminating": True},
}


def _list(*endpoints: Mapping) -> dict:
    """One EndpointSlice list holding one slice with `endpoints`, or no slice for none."""
    return {"items": [{"endpoints": list(endpoints)}] if endpoints else []}


class Slices(pihole_sibling_guard.Kube):
    """A fixture API server: each Service's list answers come off its own queue.

    The last answer in a queue repeats once the queue runs dry, so a sibling that never comes
    back is one answer.
    """

    def __init__(self, instance: list[dict], sibling: list[dict], status: int = 200):
        super().__init__("https://api", "token")
        self.answers = {"pihole": instance, "pihole-2-web": sibling}
        self.status = status
        self.reads: list[str] = []

    def get(self, path: str) -> tuple[int, dict]:
        for service, queue in self.answers.items():
            if path == pihole_sibling_guard.slices_path(NAMESPACE, service):
                self.reads.append(service)
                if self.status != 200:
                    return self.status, {"message": "endpointslices is forbidden"}
                return 200, queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"unexpected read {path}")


class Clock:
    """A monotonic clock that `sleep` advances, so a 300s wait runs in no time."""

    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def _main(kube: Slices) -> int:
    clock = Clock()
    return pihole_sibling_guard.main([], ENV, kube, clock.sleep, clock)


def test_a_ready_sibling_lets_instance_1_roll(capsys):
    kube = Slices([_list(READY)], [_list(READY)])
    assert _main(kube) == 0
    assert "pihole-2-web has 1 ready endpoint(s), so pihole may roll" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "sibling",
    [_list(), _list(NOT_READY), _list(TERMINATING)],
    ids=["no-slice", "not-ready", "terminating"],
)
def test_a_sibling_with_no_ready_endpoint_holds_instance_1(sibling, capsys):
    kube = Slices([_list(READY)], [sibling])
    assert _main(kube) == 1
    assert (
        "pihole-2-web had no ready endpoint for 300s, so rolling pihole would leave the LAN "
        "with no resolver" in capsys.readouterr().err
    )
    # It polled through the wait rather than failing on the first read.
    assert kube.reads.count("pihole-2-web") > 1


def test_a_sibling_that_comes_back_inside_the_wait_lets_instance_1_roll():
    kube = Slices([_list(READY)], [_list(), _list(NOT_READY), _list(READY)])
    assert _main(kube) == 0
    assert kube.reads == ["pihole", "pihole-2-web", "pihole-2-web", "pihole-2-web"]


@pytest.mark.parametrize(
    "instance", [_list(), _list(NOT_READY)], ids=["first-apply", "not-serving"]
)
def test_instance_1_serving_nothing_passes_without_reading_the_sibling(
    instance, capsys
):
    """A first apply: pihole-2 exists only after `pihole-deferred`, which waits on `pihole`."""
    kube = Slices([instance], [_list()])
    assert _main(kube) == 0
    assert kube.reads == ["pihole"]
    assert "rolling it takes no DNS away" in capsys.readouterr().out


def test_an_api_error_fails_at_once(capsys):
    kube = Slices([_list(READY)], [_list(READY)], status=403)
    assert _main(kube) == 1
    assert kube.reads == ["pihole"]
    assert "answered HTTP 403: endpointslices is forbidden" in capsys.readouterr().err


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


@pytest.fixture(scope="module")
def guard_tag(stubs, tmp_path_factory) -> cluster.RenderedTag:
    """The render of a role copy whose verify directory holds both Job forms."""
    tree = tmp_path_factory.mktemp("guard") / "ansible"
    role = tree / "roles" / "k8s" / TAG
    shutil.copytree(K8S_ROLES / TAG, role)
    (tree / "vars").mkdir()
    shutil.copy(ANSIBLE / "vars" / "cluster-vars.source", tree / "vars")
    (role / "templates" / "verify-inert").rename(role / "templates" / "verify")
    for path in (role / "templates" / "guard-inert").iterdir():
        path.rename(role / "templates" / "verify" / f"guard-{path.name}")
    copied = {**stubs, "playbook_dir": str(tree)}
    return cluster.render_tag(TAG, copied, roles=tree / "roles" / "k8s")


@pytest.fixture(scope="module")
def guard_render(guard_tag) -> dict[str, str]:
    return guard_tag.verify


def _guard_docs(files: dict[str, str]) -> list[dict]:
    return [
        doc
        for name, text in sorted(files.items())
        if name.startswith("guard-")
        for doc in yaml_fast.safe_load_all(text)
        if doc
    ]


def _jobs(files: dict[str, str]) -> dict[str, dict]:
    return {
        doc["metadata"]["name"].rsplit("-", 1)[0]: doc
        for text in files.values()
        for doc in yaml_fast.safe_load_all(text)
        if doc and doc["kind"] == "Job"
    }


def _env(job: dict) -> dict[str, str]:
    (container,) = job["spec"]["template"]["spec"]["containers"]
    return {env["name"]: env.get("value") for env in container["env"]}


def test_the_tree_renders_no_guard_while_ansible_owns_pihole(stubs):
    rendered = cluster.render_tag(TAG, stubs).directories()
    assert [
        f"{directory}/{name}"
        for directory, files in rendered.items()
        for name, text in files.items()
        if "pihole-sibling-guard" in text
    ] == []


def test_the_guard_passes_every_job_census_row_but_kind(guard_render):
    """`kind` is out: the pre-apply kind the guard belongs in does not exist yet (#5719).

    Rendered into `<tag>-verify`, the guard would pass `kind` as a verify Job, which is the
    wrong place for it, so a pass there would prove nothing about where it runs.
    """
    docs = _guard_docs(guard_render)
    assert sorted(doc["kind"] for doc in docs) == [
        "ConfigMap",
        "Job",
        "Role",
        "RoleBinding",
        "ServiceAccount",
    ]
    rows = {name: row for name, row in ROWS.items() if name != "kind"}
    assert problems({f"{TAG}-verify": docs}, rows) == []
    (job,) = [doc for doc in docs if doc["kind"] == "Job"]
    assert job["spec"]["backoffLimit"] == 0


def test_the_guard_reads_the_two_single_instance_services(guard_tag, guard_render):
    """`pihole-dns` selects both instances, so instance 1's own endpoint would satisfy it."""
    env = _env(_jobs(guard_render)["pihole-sibling-guard"])
    assert (env["GUARD_INSTANCE_SERVICE"], env["GUARD_SIBLING_SERVICE"]) == (
        "pihole",
        "pihole-2-web",
    )
    services = {
        doc["metadata"]["name"]: doc["spec"]["selector"]
        for text in guard_tag.files.values()
        for doc in yaml_fast.safe_load_all(text)
        if doc and doc["kind"] == "Service"
    }
    assert services["pihole"]["instance"] == "pihole"
    assert services["pihole-2-web"]["instance"] == "pihole-2"


def test_the_guard_renames_with_the_gravity_job(guard_render):
    """Both Jobs embed one roll digest, so a revision that re-runs one re-runs the other."""
    jobs = _jobs(guard_render)
    assert set(jobs) == {"pihole-gravity", "pihole-sibling-guard"}
    digests = {name: _env(job)["PIHOLE_ROLL_DIGEST8"] for name, job in jobs.items()}
    assert len(set(digests.values())) == 1, digests
