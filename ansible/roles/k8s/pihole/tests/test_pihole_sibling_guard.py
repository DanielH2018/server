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
from collections.abc import Mapping, Sequence

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
    "PIHOLE_INSTANCE_1_ROLL_DIGEST8": "abcd1234",
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


# The empty slice the EndpointSlice controller keeps for a Service with no endpoints.
PLACEHOLDER = {"items": [{"endpoints": None}]}


class Slices(pihole_sibling_guard.Kube):
    """A fixture API server: each Service's list answers come off its own queue.

    The last answer in a queue repeats once the queue runs dry, so a sibling that never comes
    back is one answer.
    """

    def __init__(
        self,
        instance: list[dict],
        sibling: list[dict],
        statuses: Sequence[int] = (200,),
    ):
        super().__init__("https://api", "token")
        self.answers = {"pihole": instance, "pihole-2-web": sibling}
        # One status per read, across both Services, the last repeating like an answer.
        self.statuses = list(statuses)
        self.reads: list[str] = []

    def get(self, path: str) -> tuple[int, dict]:
        for service, queue in self.answers.items():
            if path == pihole_sibling_guard.slices_path(NAMESPACE, service):
                self.reads.append(service)
                statuses = self.statuses
                status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
                if status != 200:
                    return status, {"message": "endpointslices is forbidden"}
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
    [_list(), PLACEHOLDER, _list(NOT_READY), _list(TERMINATING)],
    ids=["no-slice", "placeholder", "not-ready", "terminating"],
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


def test_a_first_apply_passes_without_reading_the_sibling(capsys):
    """No slice for `pihole`: pihole-2 exists only after `pihole-deferred`, which waits on `pihole`."""
    kube = Slices([_list()], [_list()])
    assert _main(kube) == 0
    assert kube.reads == ["pihole"]
    assert "pihole has no EndpointSlice, so this is its first apply" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "instance", [PLACEHOLDER, _list(NOT_READY)], ids=["placeholder", "not-ready"]
)
def test_an_instance_1_serving_nothing_still_waits_on_the_sibling(instance):
    """Only a missing slice is a first apply; a Service with no ready pod is held too."""
    kube = Slices([instance], [_list()])
    assert _main(kube) == 1
    assert kube.reads.count("pihole-2-web") > 1


def test_an_api_error_fails_at_once(capsys):
    kube = Slices([_list(READY)], [_list(READY)], statuses=[403])
    assert _main(kube) == 1
    assert kube.reads == ["pihole"]
    assert "answered HTTP 403: endpointslices is forbidden" in capsys.readouterr().err


@pytest.mark.parametrize("status", [0, 429, 503], ids=["no-answer", "429", "503"])
def test_a_transient_read_is_retried_inside_the_wait(status):
    """An API blip must not fail a guard whose sibling is serving: its name would not change."""
    kube = Slices([_list(READY)], [_list(READY)], statuses=[200, status, status, 200])
    assert _main(kube) == 0
    assert kube.reads == ["pihole", "pihole-2-web", "pihole-2-web", "pihole-2-web"]


def test_a_read_that_never_answers_fails_at_the_end_of_the_wait(capsys):
    kube = Slices([_list(READY)], [_list(READY)], statuses=[200, 0])
    assert _main(kube) == 1
    assert "pihole-2-web's EndpointSlices got no answer" in capsys.readouterr().err
    assert kube.reads.count("pihole-2-web") > 1


def test_a_transport_error_reads_as_no_answer():
    """`open_url` re-raises a read timeout or a reset; `get` turns it into status 0."""

    def opener(request, timeout):
        raise TimeoutError("timed out")

    kube = pihole_sibling_guard.Kube("https://api", "token", opener)
    assert kube.get("/apis") == (0, {"message": "TimeoutError('timed out')"})


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _render_copy(tmp_path, stubs, edit=None) -> cluster.RenderedTag:
    """The render of a role copy whose verify directory holds both Job forms.

    `edit(role)` changes the copied role first.
    """
    tree = tmp_path / "ansible"
    role = tree / "roles" / "k8s" / TAG
    shutil.copytree(K8S_ROLES / TAG, role)
    (tree / "vars").mkdir()
    shutil.copy(ANSIBLE / "vars" / "cluster-vars.source", tree / "vars")
    (role / "templates" / "verify-inert").rename(role / "templates" / "verify")
    for path in (role / "templates" / "guard-inert").iterdir():
        path.rename(role / "templates" / "verify" / f"guard-{path.name}")
    if edit:
        edit(role)
    copied = {**stubs, "playbook_dir": str(tree)}
    return cluster.render_tag(TAG, copied, roles=tree / "roles" / "k8s")


@pytest.fixture(scope="module")
def guard_tag(stubs, tmp_path_factory) -> cluster.RenderedTag:
    return _render_copy(tmp_path_factory.mktemp("guard"), stubs)


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


def _edit(name: str, old: str, new: str):
    def edit(role) -> None:
        path = role / "templates" / name
        text = path.read_text()
        assert old in text, name
        path.write_text(text.replace(old, new))

    return edit


# Each edit, and whether it rolls instance 1 under Flux.
EDITS = {
    "instance-1-deployment": (
        _edit("deployment.yaml.j2", "pihole_k8s_claim)", "'pihole-etc-moved')"),
        True,
    ),
    "shared-config": (
        _edit("config/pihole-dnsmasq.conf.j2", "\n", "\n# a dnsmasq-only edit\n"),
        True,
    ),
    "instance-2-deployment": (
        _edit("deployment-2.yaml.j2", "pihole_k8s_claim_2)", "'pihole-etc-2-moved')"),
        False,
    ),
    "yaml-comment": (
        _edit("deployment.yaml.j2", "---\n", "---\n# a comment that rolls nothing\n"),
        False,
    ),
}


@pytest.mark.parametrize("case", sorted(EDITS))
def test_the_guard_renames_exactly_when_instance_1_rolls(
    case, stubs, guard_tag, tmp_path
):
    """An instance-2-only fix keeps the name, so a down pihole-2 cannot hold its own fix."""
    edit, renames = EDITS[case]
    before = _jobs(guard_tag.verify)
    after = _jobs(_render_copy(tmp_path, stubs, edit).verify)
    # The edit reached the render: the gravity Job reads every roll input of both instances.
    if case != "yaml-comment":
        assert after["pihole-gravity"] != before["pihole-gravity"]
    renamed = (
        after["pihole-sibling-guard"]["metadata"]["name"]
        != before["pihole-sibling-guard"]["metadata"]["name"]
    )
    assert renamed is renames
