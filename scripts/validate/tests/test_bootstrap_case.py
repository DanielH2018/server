"""The bootstrap container cases' verdicts and kit, without a container (H7.15a).

The cases themselves run only in CI's `host_packages` job, which has Docker. This holds what
they read from the script and decide from a container's output, with a green and a red input
for each, and holds that the job runs them.

Run: uv run pytest scripts/validate/tests/test_bootstrap_case.py
"""

import subprocess
from pathlib import Path

import pytest
from lib.git import git
from validate import host_package_cases
from validate.validate_lib import bootstrap_case as case
from validate.validate_lib import bootstrap_repo_case as repo_case
from validate.validate_lib import host_fixtures
from validate.validate_lib.host_container import CaseFailed, Container

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

    planned = case.plan(tmp_path, green)

    online = [p.host for p in planned if p.kit is None]
    assert {"daniel-box", "daniel-server", "daniel-pi"} <= set(online)
    assert online == case.script_value(SCRIPT, "HOSTS").split()
    assert all(p.marker == case.ANCHOR_ACCEPTS for p in planned if p.kit is None)
    offline = [(p, p.kit) for p in planned if p.kit is not None]
    assert [(kit.name, p.refusal is None) for p, kit in offline] == [
        ("green", True),
        ("red-k3s", False),
        ("red-nfpm_2.48.0_Linux_x86_64.tar.gz", False),
        ("green", False),
    ]
    assert not (offline[1][1] / "k3s").exists()
    assert [p.name for p, _ in offline[1:]] == [
        "refuse a kit whose k3s is missing",
        "refuse a kit whose nfpm_2.48.0_Linux_x86_64.tar.gz does not match its pin",
        "refuse a local build whose homelab-daniel-box Depends on homelab-daniel-server",
    ]
    depends = offline[3][0]
    assert (depends.clone, depends.refusal) == ("red-depends", case.INSTALL_REFUSED)
    assert depends.apt_says == "Depends: homelab-daniel-server"
    assert {p.clone for p in planned[:-1]} == {"green"}


def test_a_depends_entry_added_to_daniel_box_s_spec_is_clean():
    spec = (case.REPO / case.BOX_SPEC).read_text()
    changed = case.add_depends(spec, "homelab-only")
    assert "depends:\n  - homelab-tools\n  - homelab-only\n" in changed
    assert changed.replace("  - homelab-only\n", "", 1) == spec


def test_a_spec_with_no_depends_list_is_flagged():
    with pytest.raises(CaseFailed, match="holds 0 depends: lists"):
        case.add_depends("description: x\ncomponents:\n  - reconcile\n", "homelab-only")


def test_the_red_clone_is_one_commit_past_the_green_one(tmp_path):
    green = tmp_path / "green"
    (green / "hosts" / "daniel-box").mkdir(parents=True)
    (green / case.BOX_SPEC).write_text("depends:\n  - homelab-tools\nfiles: []\n")
    case._git("init", "-q", cwd=green)
    case._git("add", "-A", cwd=green)
    case._git("commit", "-q", "-m", "green", cwd=green)

    red = case.red_clone(green, tmp_path / "red")

    log = git("log", "--format=%s", cwd=red).stdout.split("\n")
    assert log[:2] == ["bootstrap case: depend on homelab-daniel-server", "green"]
    assert "  - homelab-daniel-server\n" in (red / case.BOX_SPEC).read_text()
    assert "homelab-daniel-server" not in (green / case.BOX_SPEC).read_text()


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


def test_the_host_packages_job_runs_the_bootstrap_cases():
    assert "bootstrap_case.run(cases)" in Path(host_package_cases.__file__).read_text()


def test_the_host_packages_job_runs_the_repository_case():
    text = Path(host_package_cases.__file__).read_text()
    assert "bootstrap_repo_case.run(cases)" in text


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
)
REPO_SIMULATION = "".join(
    f"Inst {package} (0.1+gabc homelab:homelab [all])\n"
    for package in ("homelab-tools", "homelab-updater", "homelab-daniel-pi")
)


def test_a_repository_install_of_the_three_packages_by_name_is_clean():
    assert repo_case.apt_problems("daniel-pi", REPO_CALLS, REPO_SIMULATION) == []


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
