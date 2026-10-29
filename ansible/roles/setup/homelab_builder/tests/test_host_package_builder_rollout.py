"""The builder's staged-rollout gate: the canary's package first, the others after it reports.

H3.12. The render writes `packages/homelab-updater` and one `hosts/<host>` package per host
file in the commit, so a commit that edits `daniel-pi` changes only the Pi's package. Prometheus
is a stub keyed by the PromQL's metric and host, and it can refuse an address. Each refusal case
is paired with the case that lets the other host's package build.

Run: uv run pytest ansible/roles/setup/homelab_builder/tests/test_host_package_builder_rollout.py
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

import builder_rollout_gate as gate
from deploy_tools.land_lib.host_schedule import calendar_after
from lib.repo_paths import REPO
from lib.systemd_timer import timer_settings
from test_host_package_builder import (
    FPR,
    GREEN,
    FakeGitHub,
    FixtureBuilder,
    Origin,
    builder as make_builder,
    git,
    metric,
    staged,
)

URLS = "http://127.0.0.1:9090 http://10.43.0.9:9090"
# A run inside the Pi's firing hours, and one inside its 23:00 to 00:59 gap: #6524's publish.
DAYTIME = datetime(2026, 10, 28, 14, 1, tzinfo=UTC).timestamp()
QUIET = datetime(2026, 10, 28, 23, 8, 33, tzinfo=UTC).timestamp()


class HostsBuilder(FixtureBuilder):
    """The updater's fixture render plus a package for each host file the commit holds."""

    def render(self, tree: Path, out: Path) -> None:
        super().render(tree, out)
        (out / "packages" / "homelab-updater" / "root" / "tree.txt").write_text("fixed")
        for host in ("daniel-pi", "daniel-server", "daniel-box"):
            if (tree / host).is_file():
                package = out / "hosts" / host
                (package / "root").mkdir(parents=True)
                (package / "nfpm.yaml").write_text(f"name: homelab-{host}\n")
                (package / "root" / "tree.txt").write_text((tree / host).read_text())


class Prom:
    """`fetch` for the rollout gate: the canary state per host, and the addresses that refuse."""

    def __init__(self):
        self.applied: dict[str, str] = {}
        self.reconcile: dict[str, float] = {}
        self.refuse: set[str] = set()
        self.asked: list[str] = []

    def __call__(self, url: str, timeout: float) -> bytes:
        base = url.split("/api/")[0]
        self.asked.append(base)
        if base in self.refuse:
            raise ConnectionRefusedError(base)
        match = re.search(r"query=(\w+)%7Bhost%3D%22([\w-]+)%22%7D", url)
        assert match, url
        name, host = match.groups()
        if name == gate.APPLIED and host in self.applied:
            result = [{"metric": {"host": host, "sha": self.applied[host]}}]
        elif name == gate.RECONCILE_OK and host in self.reconcile:
            result = [
                {"metric": {"host": host}, "value": [0, str(self.reconcile[host])]}
            ]
        else:
            result = []
        if name == gate.APPLIED:
            result = [dict(r, value=[0, "1"]) for r in result]
        return json.dumps({"status": "success", "data": {"result": result}}).encode()


def edit(origin, host: str, text: str) -> str:
    """A commit that sets the host file's text, one second after the last commit."""
    origin.clock += 1
    (origin.path / host).write_text(text)
    git(origin.path, "add", host)
    origin._env_git("commit", "--quiet", "-m", f"{host} {text}")
    return git(origin.path, "rev-parse", "HEAD")


def run(lab, prom: Prom, at: float = DAYTIME, **overrides) -> FixtureBuilder:
    """One builder run at `at`, with every commit green and `prom` as its Prometheus."""
    origin, _state, etc, github = lab
    for sha in git(origin.path, "rev-list", "master").split():
        github.runs[sha] = GREEN
    b = make_builder(lab, cls=HostsBuilder, PROMETHEUS_URLS=URLS, **overrides)
    b.release_path = etc / "release"
    b.prom_fetch = prom
    b.now = lambda: at
    assert b.main() == 0
    return b


def sign(state: Path) -> None:
    """The signer took the staged set, as H3.5b's does after every run."""
    for entry in (state / "staged").iterdir():
        entry.unlink()


def debs(state: Path) -> list[str]:
    """The staged packages by Debian name; the fixture names a .deb for its directory."""
    names = (n.split("_")[0] for n in staged(state) if n.endswith(".deb"))
    return sorted(n if n.startswith("homelab-") else f"homelab-{n}" for n in names)


@pytest.fixture
def lab(tmp_path):
    origin = Origin(tmp_path / "origin")
    state, etc = tmp_path / "state", tmp_path / "etc"
    (state / "staged").mkdir(parents=True)
    etc.mkdir()
    (etc / "archive-key.fpr").write_text(FPR + "\n")
    return origin, state, etc, FakeGitHub()


@pytest.fixture
def opened(lab):
    """A lab whose first run changed the Pi's and the server's packages: the gate is open."""
    origin, state, etc, _ = lab
    edit(origin, "daniel-pi", "pi 1")
    v = edit(origin, "daniel-server", "server 1")
    prom = Prom()
    run(lab, prom)
    sign(state)
    return lab, prom, v, state, etc


def test_two_changed_host_packages_stage_the_canary_and_the_shared_one(opened):
    _, _, v, state, _ = opened
    # The first run also built the shared updater, which the gate never holds back.
    assert json.loads((state / "state.json").read_text())["staged"] == [
        "hosts/daniel-pi",
        "packages/homelab-updater",
    ]
    assert json.loads((state / "state.json").read_text())["deferred"] == [
        "hosts/daniel-server"
    ]
    reason, sha, since = metric(state)
    assert (reason, sha) == ("canary", v[:12])
    assert since == float(git(state / "src", "show", "-s", "--format=%ct", v))


def test_a_single_changed_host_package_opens_no_gate(lab):
    origin, state, _, _ = lab
    edit(origin, "daniel-pi", "pi 1")
    run(lab, Prom())
    assert debs(state) == ["homelab-daniel-pi", "homelab-updater"]
    assert metric(state)[0] == "ok"
    assert not json.loads((state / "state.json").read_text()).get("gate")


def test_the_canary_reporting_at_the_gate_s_version_builds_the_deferred_package(opened):
    lab, prom, v, state, _ = opened
    prom.applied["daniel-pi"], prom.reconcile["daniel-pi"] = v[:12], 1
    run(lab, prom)
    assert debs(state) == ["homelab-daniel-server"]
    # The deferred package names the commit that staged/chosen names, as the signer requires.
    assert (state / "staged" / "chosen").read_text() == v[:12] + "\n"
    assert metric(state)[0] == "ok"


@pytest.mark.parametrize("case", ["reconcile-0", "older-sha", "no-series"])
def test_a_canary_not_yet_settled_leaves_the_gate_open(opened, case):
    lab, prom, v, state, _ = opened
    older = git(lab[0].path, "rev-parse", f"{v}~1")[:12]
    prom.applied["daniel-pi"] = older if case == "older-sha" else v[:12]
    prom.reconcile["daniel-pi"] = 0 if case == "reconcile-0" else 1
    if case == "no-series":
        prom.applied.clear()
    run(lab, prom)
    assert debs(state) == []
    assert metric(state)[:2] == ("canary", v[:12])


def test_a_canary_at_a_descendant_closes_the_gate(opened):
    lab, prom, _v, state, _ = opened
    newer = edit(lab[0], "daniel-box", "box 1")
    prom.applied["daniel-pi"], prom.reconcile["daniel-pi"] = newer[:12], 1
    # The tip moved, so this run builds the new commit and every package still owed.
    run(lab, prom)
    assert "homelab-daniel-server" in debs(state)


def test_no_answer_on_either_address_keeps_the_gate_open(opened):
    lab, prom, v, state, _ = opened
    prom.applied["daniel-pi"], prom.reconcile["daniel-pi"] = v[:12], 1
    prom.refuse = set(URLS.split())
    run(lab, prom)
    assert debs(state) == []
    assert metric(state)[0] == "canary"
    assert set(prom.asked) == set(URLS.split()), "both addresses were tried"


def test_a_refused_loopback_falls_back_to_the_cluster_ip(opened):
    lab, prom, v, state, _ = opened
    prom.applied["daniel-pi"], prom.reconcile["daniel-pi"] = v[:12], 1
    prom.refuse = {"http://127.0.0.1:9090"}
    run(lab, prom)
    assert debs(state) == ["homelab-daniel-server"]


def test_a_release_file_naming_the_gate_s_commit_closes_it(opened):
    lab, prom, v, state, etc = opened
    (etc / "release").write_text(v + "\n")
    run(lab, prom)
    assert debs(state) == ["homelab-daniel-server"]
    assert prom.asked == [], "the override closes the gate before Prometheus is asked"


def test_a_release_file_naming_an_older_commit_leaves_it_open(opened):
    lab, prom, v, state, etc = opened
    (etc / "release").write_text(git(lab[0].path, "rev-parse", f"{v}~1") + "\n")
    run(lab, prom)
    assert debs(state) == []
    assert metric(state)[0] == "canary"


def test_a_release_file_naming_a_descendant_closes_it(opened):
    lab, prom, _v, state, etc = opened
    (etc / "release").write_text(edit(lab[0], "daniel-box", "box 1") + "\n")
    run(lab, prom)
    assert "homelab-daniel-server" in debs(state)


@pytest.mark.parametrize("text", ["", "not-a-sha\n", "--all\n"])
def test_a_release_file_that_is_not_a_sha_leaves_it_open(opened, text):
    lab, prom, _, state, etc = opened
    (etc / "release").write_text(text)
    run(lab, prom)
    assert debs(state) == []


def test_a_newer_canary_version_moves_the_gate(opened):
    lab, prom, _v, state, _ = opened
    newer = edit(lab[0], "daniel-pi", "pi 2")
    run(lab, prom)
    assert debs(state) == ["homelab-daniel-pi"]
    assert metric(state)[:2] == ("canary", newer[:12])
    assert json.loads((state / "state.json").read_text())["gate"]["sha"] == newer


def test_without_the_pi_s_package_daniel_server_is_the_canary(lab):
    origin, state, _, _ = lab
    edit(origin, "daniel-box", "box 1")
    edit(origin, "daniel-server", "server 1")
    run(lab, Prom())
    assert debs(state) == ["homelab-daniel-server", "homelab-updater"]
    assert json.loads((state / "state.json").read_text())["gate"]["host"] == (
        "daniel-server"
    )


@pytest.mark.parametrize(
    ("changed", "staged_now", "deferred"),
    [
        (
            ["hosts/daniel-box", "hosts/daniel-pi"],
            ["hosts/daniel-pi"],
            ["hosts/daniel-box"],
        ),
        (
            ["hosts/daniel-box", "hosts/daniel-server"],
            ["hosts/daniel-server"],
            ["hosts/daniel-box"],
        ),
        (["hosts/new", "hosts/daniel-box"], ["hosts/daniel-box"], ["hosts/new"]),
        (
            ["packages/homelab-tools", "hosts/daniel-box"],
            ["packages/homelab-tools", "hosts/daniel-box"],
            [],
        ),
    ],
)
def test_the_canary_order_and_the_shared_packages(changed, staged_now, deferred):
    got = gate.admit(changed, None, "a" * 40, DAYTIME)
    assert (got[0], got[1]) == (staged_now, deferred)


def test_a_pi_package_published_in_its_quiet_hours_makes_daniel_server_the_canary(lab):
    """#6524: a canary that next fires at 01:05 held daniel-server's package for 2h04m."""
    origin, state, _, _ = lab
    edit(origin, "daniel-pi", "pi 1")
    edit(origin, "daniel-server", "server 1")
    run(lab, Prom(), at=QUIET)
    assert debs(state) == ["homelab-daniel-server", "homelab-updater"]
    saved = json.loads((state / "state.json").read_text())
    assert saved["gate"]["host"] == "daniel-server"
    assert saved["deferred"] == ["hosts/daniel-pi"]


def test_a_newer_pi_version_in_its_quiet_hours_re_picks_the_open_gate(opened):
    lab, prom, _v, state, _ = opened
    newer = edit(lab[0], "daniel-pi", "pi 2")
    run(lab, prom, at=QUIET)
    assert debs(state) == ["homelab-daniel-server"]
    saved = json.loads((state / "state.json").read_text())
    assert saved["gate"] == {"host": "daniel-server", "sha": newer}
    assert saved["deferred"] == ["hosts/daniel-pi"]


def test_an_open_pi_gate_closes_with_master_idle_once_the_pi_stops_firing(opened):
    """The Pi's package published after its 22:50 fire, and nothing new reached master.

    daniel-server is the only deferred package, so it builds with no gate of its own.
    """
    lab, prom, _v, state, _ = opened
    run(lab, prom, at=QUIET)
    assert debs(state) == ["homelab-daniel-server"]
    saved = json.loads((state / "state.json").read_text())
    assert (saved["gate"], saved["deferred"]) == (None, [])


def test_the_fire_table_is_every_host_drop_in_s_schedule():
    """CALENDAR_FIRES restates each drop-in, which the builder cannot read, fire for fire."""
    dropins = sorted(REPO.glob("hosts/*/units/homelab-updater.timer.conf"))
    shared = (
        REPO / "hosts/packages/homelab-updater/units/homelab-updater.timer"
    ).read_text()
    assert {p.parts[-3] for p in dropins} == set(gate.CALENDAR_FIRES)
    for dropin in dropins:
        triggers, _ = timer_settings(shared, [dropin.read_text()])
        assert set(triggers) == {"OnCalendar"}, dropin
        day = calendar_after(triggers["OnCalendar"], DAYTIME - DAYTIME % 86400, 200)
        day = [t for t in day if t < DAYTIME - DAYTIME % 86400 + 86400]
        host = dropin.parts[-3]
        walk, t = [], day[0] - 1
        while (t := gate.next_fire(host, t)) < day[-1] + 1:
            walk.append(t)
        assert walk == day, host


def test_the_bound_covers_the_pi_s_spacing_and_stays_under_the_lag_arm():
    """The Pi stays the canary all through its firing hours, and a gate on any canary closes
    before monitor-bridge's lag arm pages: the wait, then the updater's longest run."""
    minutes = gate.CALENDAR_FIRES["daniel-pi"][1]
    spacing = max(
        b - a for a, b in zip(minutes, (*minutes[1:], minutes[0] + 60), strict=True)
    )
    assert spacing * 60 <= gate.CANARY_MAX_WAIT_S
    unit = (
        REPO / "hosts/packages/homelab-updater/units/homelab-updater.service"
    ).read_text()
    timeout = re.search(r"^TimeoutStartSec=(\d+)min$", unit, re.M)
    assert timeout
    timeout_min = int(timeout[1])
    defaults = (REPO / "ansible/roles/k8s/monitor-bridge/defaults/main.yml").read_text()
    found = re.search(r"^monitor_bridge_builder_max_lag_s: (\d+)$", defaults, re.M)
    assert found
    lag = int(found[1])
    assert gate.CANARY_MAX_WAIT_S + timeout_min * 60 < lag
