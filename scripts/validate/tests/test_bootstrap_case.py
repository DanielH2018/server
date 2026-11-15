"""The bootstrap container cases' verdicts and kit, without a container (H7.15a).

The cases themselves run only in CI's `host_packages` job, which has Docker. This holds what
they read from the script and decide from a container's output, with a green and a red input
for each, and holds that the job runs them.

Run: uv run pytest scripts/validate/tests/test_bootstrap_case.py
"""

import argparse
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest
from lib import yaml_fast
from lib.git import git
from lib.repo_paths import REPO
from validate import host_package_cases
from validate.validate_lib import bootstrap_age_case as age_case
from validate.validate_lib import case_groups
from validate.validate_lib import bootstrap_case as case
from validate.validate_lib import bootstrap_repo_case as repo_case
from validate.validate_lib import fixture_host_case as fixture_case
from validate.validate_lib import scratch_clones as clones
from validate.validate_lib import host_fixtures
from validate.validate_lib.host_container import CaseFailed, Container, uid_problems

SCRIPT = case.SCRIPT.read_text()
DEBS = (
    "/tmp/w/debs/homelab-tools_0.1+gabc_amd64.deb"
    " /tmp/w/debs/homelab-updater_0.1+gabc_all.deb"
    " /tmp/w/debs/homelab-daniel-box_0.1+gabc_all.deb"
)
CALLS = f"update -o Dir::Etc::sourceparts=/tmp/w/sources.list.d\ninstall -y {DEBS}\n"
SIMULATION = "".join(
    f"Inst {package} (0.1+gabc local-deb [all])\n"
    for package in ("homelab-tools", "homelab-updater", "homelab-daniel-box")
)


def test_the_kit_pins_name_the_three_files_the_offline_branch_checks():
    pins = case.kit_pins(SCRIPT)
    assert set(pins) == {
        "install.sh",
        "nfpm_2.48.0_Linux_x86_64.tar.gz",
        "uv-x86_64-unknown-linux-gnu.tar.gz",
    }
    assert pins["install.sh"][1] == case.script_value(SCRIPT, "K3S_INSTALL_SHA256")
    assert all(len(sha256) == 64 for _, sha256 in pins.values())


def test_a_script_whose_pins_moved_is_refused_rather_than_read_as_no_pin():
    with pytest.raises(CaseFailed, match="no amd64 uv"):
        case.kit_pins(SCRIPT.replace("  uv:\n    amd64:", "  uv:\n    x86:"))
    with pytest.raises(CaseFailed, match="sets no K3S_INSTALL_URL"):
        case.kit_pins(SCRIPT.replace("K3S_INSTALL_URL=", "K3S_URL_OF_INSTALLER="))


def test_the_plan_runs_every_host_online_then_offline_with_three_reds(tmp_path):
    green = tmp_path / "bootstrap-kit" / "green"
    green.mkdir(parents=True)
    (green / "k3s").write_text("k3s")

    planned = case.selected(case.plan(tmp_path, green), k3s_download=False)

    online = [p.host for p in planned if p.kit is None]
    assert {"daniel-box", "daniel-server", "daniel-pi"} <= set(online)
    assert online == case.script_value(SCRIPT, "HOSTS").split()
    assert all(p.marker == case.ANCHOR_ACCEPTS for p in planned if p.kit is None)
    assert all(p.seed == tmp_path / case.SEED_DIR for p in planned if p.kit is None)
    assert all(p.seed is None for p in planned if p.kit is not None)
    offline = [(p, p.kit) for p in planned if p.kit is not None]
    assert [(kit.name, p.refusal is None) for p, kit in offline] == [
        ("green", True),
        ("red-k3s", False),
        ("red-nfpm_2.48.0_Linux_x86_64.tar.gz", False),
        ("green", False),
        ("green", True),
    ]
    assert not (offline[1][1] / "k3s").exists()
    assert [p.name for p, _ in offline[1:4]] == [
        "refuse a kit whose k3s is missing",
        "refuse a kit whose nfpm_2.48.0_Linux_x86_64.tar.gz does not match its pin",
        "refuse a local build whose homelab-daniel-box Depends on homelab-daniel-server",
    ]
    depends = offline[3][0]
    assert (depends.clone, depends.refusal) == ("red-depends", case.INSTALL_REFUSED)
    assert depends.apt_says == "Depends: homelab-daniel-server"
    fixture = offline[4][0]
    assert (fixture.clone, fixture.host) == ("fixture-host", fixture_case.FIXTURE_HOST)
    assert fixture.marker == case.UNCHECKED
    assert {p.clone for p in planned[:-2]} == {"green"}


def _gating(tmp_path):
    green = tmp_path / "bootstrap-kit" / "green"
    green.mkdir(parents=True)
    (green / "k3s").write_text("k3s")
    return case.selected(case.plan(tmp_path, green), k3s_download=False)


def test_each_shard_holds_one_offline_build_and_the_shards_cover_the_plan_once(
    tmp_path,
):
    planned = _gating(tmp_path)
    builds = [p.label for p in planned if p.builds_offline]
    shards = [case.shard(planned, k, len(builds)) for k in range(1, len(builds) + 1)]

    assert set(builds) == {"offline", "red-depends", "fixture-host"}
    assert [[p.label for p in s if p.builds_offline] for s in shards] == [
        [label] for label in builds
    ]
    dealt = [p.label for s in shards for p in s]
    assert sorted(dealt) == sorted(p.label for p in planned)
    assert all(s == [p for p in planned if p in s] for s in shards)


def test_ci_runs_one_bootstrap_shard_per_offline_build_on_each_release(tmp_path):
    builds = sum(p.builds_offline for p in _gating(tmp_path))
    workflow = yaml_fast.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    legs = workflow["jobs"]["host_packages_bootstrap"]["strategy"]["matrix"]["include"]
    shards: dict[str, list[str]] = {}
    for leg in legs:
        shards.setdefault(leg["release"], []).append(leg["shard"])

    assert len(shards) >= 2
    expected = [f"{k}/{builds}" for k in range(1, builds + 1)]
    assert {release: sorted(s) for release, s in shards.items()} == {
        release: expected for release in shards
    }


def test_a_shard_from_one_to_its_count_is_parsed():
    assert host_package_cases.shard_arg("2/3") == (2, 3)
    assert host_package_cases.shard_arg("3/3") == (3, 3)


@pytest.mark.parametrize("text", ["0/3", "4/3", "2", "a/3", "2/"])
def test_a_shard_outside_its_count_or_not_k_of_n_is_refused(text):
    with pytest.raises(argparse.ArgumentTypeError):
        host_package_cases.shard_arg(text)


def _unit_state_run(monkeypatch, tmp_path, host, answers):
    """A Bootstrap for `host` whose container answers `systemctl show` from `answers`."""
    cases = SimpleNamespace(image="img", work=tmp_path, python="3.12")
    run = case.Bootstrap(cases, host, tmp_path, None, "unit")

    def ok(command):
        if not command.startswith("systemctl show"):
            return ""
        prop, unit = command.split()[-2:]
        return answers[prop] if unit == case.UNITS.get(host) else "not-found"

    monkeypatch.setattr(run.container, "ok", ok)
    return run


LEFT_STOPPED = {
    "LoadState": "loaded",
    "UnitFileState": "disabled",
    "ActiveState": "inactive",
}


def test_a_node_whose_k3s_unit_is_left_disabled_and_stopped_is_clean(
    monkeypatch, tmp_path
):
    run = _unit_state_run(monkeypatch, tmp_path, "daniel-box", LEFT_STOPPED)

    assert run.unit_problems() == []


@pytest.mark.parametrize(
    ("prop", "value"), [("UnitFileState", "enabled"), ("ActiveState", "active")]
)
def test_a_node_whose_k3s_unit_is_enabled_or_running_is_flagged(
    monkeypatch, tmp_path, prop, value
):
    run = _unit_state_run(
        monkeypatch, tmp_path, "daniel-server", LEFT_STOPPED | {prop: value}
    )

    assert run.unit_problems() == [
        f"k3s-agent.service's {prop} is {value}, not {LEFT_STOPPED[prop]}"
    ]


PI_PASSWD = (
    "ubuntu:x:1000:1000::/home/ubuntu:/bin/bash\n"
    "node_exporter:x:904:904::/home/node_exporter:/usr/sbin/nologin\n"
)


def _baseline_run(monkeypatch, tmp_path, passwd: str):
    run = case.Bootstrap(
        SimpleNamespace(image="img", work=tmp_path, python="3.12"),
        "daniel-pi",
        tmp_path,
        None,
        "pi",
    )
    answers = {
        "id -nG ubuntu": "ubuntu ssh-users",
        "getent passwd": passwd,
        "systemctl is-enabled networkd-dispatcher.service || true": "masked",
    }
    monkeypatch.setattr(run.container, "ok", answers.__getitem__)
    monkeypatch.setattr(
        run.container, "read", lambda path: "/swapfile none swap sw 0 0\n"
    )
    return run


def test_a_guest_whose_declared_users_hold_their_uids_is_clean(monkeypatch, tmp_path):
    assert _baseline_run(monkeypatch, tmp_path, PI_PASSWD).baseline_problems() == []


def test_a_declared_user_left_at_another_uid_after_step_5_is_flagged(
    monkeypatch, tmp_path
):
    run = _baseline_run(monkeypatch, tmp_path, PI_PASSWD.replace(":904:", ":999:"))

    assert run.baseline_problems() == [
        "node_exporter has uid 999, not the declared 904"
    ]


def test_a_declared_user_step_5_never_created_is_flagged():
    assert uid_problems(
        'u claude 900 "Claude agent" /var/lib/claude /bin/bash\n', ""
    ) == ["claude has uid none, not the declared 900"]


def test_the_fixture_clone_is_one_commit_past_the_green_one_and_builds_a_new_host(
    tmp_path,
):
    green = tmp_path / "green"
    for relative in (
        fixture_case.WALKTHROUGH,
        fixture_case.EXAMPLE_VARS,
        fixture_case.SCRIPT,
    ):
        (green / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, green / relative)
    clones.case_git("init", "-q", cwd=green)
    clones.case_git("add", "-A", "-f", cwd=green)
    clones.case_git("commit", "-q", "-m", "base", cwd=green)

    out = clones.fixture_clone(green, tmp_path / "fixture")

    assert git("rev-list", "--count", "HEAD", cwd=out).stdout.strip() == "2"
    assert (out / "hosts" / fixture_case.FIXTURE_HOST / "package.yml").is_file()


def test_a_depends_entry_added_to_daniel_box_s_spec_is_clean():
    spec = (REPO / clones.BOX_SPEC).read_text()
    changed = clones.add_depends(spec, "homelab-only")
    assert "depends:\n  - homelab-tools\n  - libyaml-dev\n  - homelab-only\n" in changed
    assert changed.replace("  - homelab-only\n", "", 1) == spec


def test_a_spec_with_no_depends_list_is_flagged():
    with pytest.raises(CaseFailed, match="holds 0 depends: lists"):
        clones.add_depends(
            "description: x\ncomponents:\n  - reconcile\n", "homelab-only"
        )


def test_the_red_clone_is_one_commit_past_the_green_one(tmp_path):
    green = tmp_path / "green"
    (green / "hosts" / "daniel-box").mkdir(parents=True)
    (green / clones.BOX_SPEC).write_text("depends:\n  - homelab-tools\nfiles: []\n")
    clones.case_git("init", "-q", cwd=green)
    clones.case_git("add", "-A", cwd=green)
    clones.case_git("commit", "-q", "-m", "green", cwd=green)

    red = clones.red_clone(green, tmp_path / "red")

    log = git("log", "--format=%s", cwd=red).stdout.split("\n")
    assert log[:2] == ["bootstrap case: depend on homelab-daniel-server", "green"]
    assert "  - homelab-daniel-server\n" in (red / clones.BOX_SPEC).read_text()
    assert "homelab-daniel-server" not in (green / clones.BOX_SPEC).read_text()


def test_a_green_run_that_prints_its_marker_and_exits_0_is_clean():
    assert case.output_problems(0, case.UNCHECKED, "", case.UNCHECKED, None) == []


def test_an_offline_run_that_called_the_anchor_is_flagged():
    stdout = f"{case.ANCHOR_ACCEPTS} abc\n{case.UNCHECKED}\n"
    assert case.output_problems(0, stdout, "", case.UNCHECKED, case.ANCHOR_ACCEPTS) == [
        f"the output holds {case.ANCHOR_ACCEPTS!r}"
    ]
    (problem,) = case.output_problems(1, case.UNCHECKED, "boom", case.UNCHECKED, None)
    assert problem.startswith("the script exited 1")


def test_one_install_of_the_three_debs_that_apt_simulates_is_clean():
    assert case.apt_problems("daniel-box", CALLS, SIMULATION) == []


def test_an_install_apt_did_not_simulate_is_flagged():
    stdout = SIMULATION.replace("Inst homelab-daniel-box", "Conf homelab-daniel-box")
    assert case.apt_problems("daniel-box", CALLS, stdout) == [
        "apt's simulation installs no homelab-daniel-box"
    ]


def test_an_install_of_another_hosts_package_or_a_second_install_is_flagged():
    other = case.apt_problems("daniel-pi", CALLS, SIMULATION)
    assert other[0].startswith("the install named")
    assert "apt's simulation installs no homelab-daniel-pi" in other
    (twice,) = case.apt_problems(
        "daniel-box", CALLS + f"install -y {DEBS}\n", SIMULATION
    )
    assert twice.startswith("apt-get ran 2 installs")
    (none,) = case.apt_problems("daniel-box", "", SIMULATION)
    assert none.startswith("apt-get ran 0 installs")


def test_a_red_kit_changes_its_one_file_and_links_the_rest(tmp_path):
    green = tmp_path / "green"
    (green / "uv-cache").mkdir(parents=True)
    for name in ("k3s", "install.sh"):
        (green / name).write_text(name)

    dropped = case.red_kit(green, tmp_path / "red-k3s", "k3s", None)
    changed = case.red_kit(green, tmp_path / "red-install", "install.sh", b"other")

    assert sorted(p.name for p in dropped.iterdir()) == ["install.sh", "uv-cache"]
    assert (dropped / "uv-cache").resolve() == (green / "uv-cache").resolve()
    assert (changed / "install.sh").read_bytes() == b"other"
    assert not (changed / "install.sh").is_symlink()
    assert (changed / "k3s").read_text() == "k3s"


def test_a_container_boots_with_the_hostname_network_and_mounts_it_is_given(
    monkeypatch, tmp_path
):
    runs = []

    def fake_run(argv, **kwargs):
        if argv[1] != "run":
            return subprocess.CompletedProcess(argv, 0)
        runs.append(argv)
        raise subprocess.SubprocessError("stop after docker run")

    monkeypatch.setattr(subprocess, "run", fake_run)
    named = Container("img", "c", tmp_path, "daniel-box", ("/a:/b:ro",), "none")
    for container in (named, Container("img", "c", tmp_path)):
        with pytest.raises(subprocess.SubprocessError):
            container.boot("3.12")
    named_run, default_run = runs
    assert named_run[named_run.index("--hostname") + 1] == "daniel-box"
    assert named_run[named_run.index("--network") + 1] == "none"
    assert "/a:/b:ro" in named_run
    assert default_run[default_run.index("--hostname") + 1] == "daniel-pi"
    assert "--network" not in default_run


def test_a_boot_that_reads_offline_before_systemd_starts_waits_for_it(
    monkeypatch, tmp_path
):
    answers = iter(["offline\n", "", "running\n", "Python 3.12.3\n"])

    def fake_sh(self, command, **kwargs):
        return subprocess.CompletedProcess(command, 0, next(answers), "")

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
    )
    monkeypatch.setattr(Container, "sh", fake_sh)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    Container("img", "c", tmp_path).boot("3.12")


def test_an_online_run_mounts_its_seed_and_names_it_to_the_script(
    monkeypatch, tmp_path
):
    commands = []

    def fake_sh(self, command, timeout=None):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(Container, "boot", lambda self, python: None)
    monkeypatch.setattr(Container, "ok", lambda self, command: "")
    monkeypatch.setattr(Container, "sh", fake_sh)
    cases = SimpleNamespace(image="img", work=tmp_path, python="3.12")
    seed = tmp_path / "seed"

    run = case.Bootstrap(cases, "daniel-box", tmp_path, None, "box", seed)
    run.run()

    assert f"{seed}:{case.TOOLS_SEED}:ro" in run.container.mounts
    assert commands[-1].endswith(
        f"--local-build /srv/clone --tools-seed {case.TOOLS_SEED}"
    )


def test_the_default_groups_hold_two_bootstrap_groups_and_leave_bootstrap_case_out():
    runs = [run for _, run in case_groups.groups(host_package_cases.Cases)]
    assert {
        "bootstrap_age_case": age_case.run in runs,
        "bootstrap_case": case.run in runs,
        "bootstrap_repo_case": repo_case.run in runs,
    } == {
        "bootstrap_age_case": True,
        "bootstrap_case": False,
        "bootstrap_repo_case": True,
    }


def test_the_rewrite_points_the_script_at_the_test_repository_and_key():
    script = repo_case.rewrite(SCRIPT, "http://127.0.0.1:8791", "ABC123")

    assert case.script_value(script, "REPO_URL") == "http://127.0.0.1:8791"
    assert case.script_value(script, "ARCHIVE_FINGERPRINT") == "ABC123"
    assert "10.0.0.215" not in script


def test_a_script_whose_repository_variables_moved_is_refused():
    with pytest.raises(CaseFailed, match="sets REPO_URL on 0 lines"):
        repo_case.rewrite(SCRIPT.replace("REPO_URL=", "ARCHIVE_URL="), "u", "f")
    twice = SCRIPT.replace(
        "ARCHIVE_FINGERPRINT=", "ARCHIVE_FINGERPRINT=x\nARCHIVE_FINGERPRINT="
    )
    with pytest.raises(CaseFailed, match="sets ARCHIVE_FINGERPRINT on 2 lines"):
        repo_case.rewrite(twice, "u", "f")


def test_a_primary_key_fingerprint_is_read_and_a_subkey_s_is_skipped():
    colons = "pub:-:255:22:K1:::::\nfpr:::::::::aa11:\nsub:-:255:18:K2:::::\nfpr:::::::::bb22:\n"
    assert repo_case.primary_fingerprints(colons) == ["AA11"]


def test_the_published_test_keyring_has_one_fingerprint_the_script_accepts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    host_fixtures.publish(repo, [])

    fingerprint = repo_case.key_fingerprint(repo / host_fixtures.TEST_KEYRING)

    assert len(fingerprint) == 40 and fingerprint == fingerprint.upper()
    with pytest.raises(CaseFailed, match="gpg cannot read"):
        repo_case.key_fingerprint(tmp_path / "missing.gpg")


REPO_CALLS = (
    "update --error-on=any -o Dir::Etc::sourcelist=sources.list.d/homelab.sources"
    " -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0\n"
    "install -y homelab-tools\n"
    "install -y homelab-updater homelab-daniel-pi\n"
    "update --error-on=any -o Dir::Etc::sourcelist=sources.list.d/azlux.sources"
    " -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0\n"
    "install -y -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confnew log2ram\n"
)
REPO_SIMULATION = "".join(
    f"Inst {package} (0.1+gabc homelab:homelab [all])\n"
    for package in ("homelab-tools", "homelab-updater", "homelab-daniel-pi")
)


def test_a_repository_install_of_the_three_packages_by_name_is_clean():
    assert repo_case.apt_problems("daniel-pi", REPO_CALLS, REPO_SIMULATION) == []


def test_a_pi_without_its_log2ram_calls_or_another_host_with_them_is_flagged():
    azlux, log2ram = REPO_CALLS.splitlines()[-2:]
    only_packages = REPO_CALLS.replace(f"{azlux}\n{log2ram}\n", "")

    assert repo_case.apt_problems("daniel-pi", only_packages, REPO_SIMULATION) == [
        "daniel-pi ran 0 log2ram calls, not 2"
    ]
    assert case.split_log2ram("daniel-box", REPO_CALLS)[1] == [
        "daniel-box ran 2 log2ram calls, not 0"
    ]


def test_a_repository_install_that_missed_the_homelab_update_or_a_package_is_flagged():
    unchecked = REPO_CALLS.replace("--error-on=any ", "")
    (problem,) = repo_case.apt_problems("daniel-pi", unchecked, REPO_SIMULATION)
    assert problem.startswith("apt-get ran updates")
    every_source = REPO_CALLS.replace(
        "sourcelist=sources.list.d/homelab.sources", "x=y"
    )
    (problem,) = repo_case.apt_problems("daniel-pi", every_source, REPO_SIMULATION)
    assert "does not read homelab.sources alone" in problem
    other_host = repo_case.apt_problems("daniel-box", REPO_CALLS, REPO_SIMULATION)
    assert other_host[0].startswith("apt-get ran installs")
    assert "apt's simulation installs no homelab-daniel-box" in other_host
