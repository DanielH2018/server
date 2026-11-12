"""The shape of `hosts/bootstrap-host.sh`'s local build on a fake host: one install of the three
packages, the lint group left out of every `uv run`, and the time each phase took.

Run: uv run pytest hosts/lib/tests/test_bootstrap_host_local_build.py
"""

import re
import subprocess

from _bootstrap_host import FakeHost


def test_the_local_build_installs_three_packages_in_one_call_and_leaves_the_clone_alone(
    tmp_path,
):
    host = FakeHost(tmp_path, hostname="daniel-box")
    host.stock_kit()
    clone = host.clone(tmp_path)

    result = host.run(
        "daniel-box", "--local-build", str(clone), "--offline", str(host.kit)
    )

    assert result.returncode == 0, result.stderr
    assert "the clone at" in result.stdout and "is unchecked" in result.stdout
    [install] = [call for call in host.apt_calls() if call.startswith("install")]
    debs = install.split()[2:]
    assert [d.rsplit("/", 1)[1].split("_")[0] for d in debs] == [
        "homelab-tools",
        "homelab-updater",
        "homelab-daniel-box",
    ]
    assert (host.root / "nfpm.versions").read_text().split()[0].startswith("0.1+g")
    assert not (host.root / "etc/apt/sources.list.d/homelab.sources").exists()
    status = subprocess.run(
        ["git", "-C", str(clone), "status", "--porcelain", "--ignored"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert status.stdout == ""


def test_the_local_build_creates_the_users_the_host_deb_declares_before_the_install(
    tmp_path,
):
    host = FakeHost(tmp_path, hostname="daniel-box")
    host.stock_kit()
    host.declare_users('u claude 996 "Claude agent" /var/lib/claude /bin/bash\n')

    result = host.run(
        "daniel-box",
        "--local-build",
        str(host.clone(tmp_path)),
        "--offline",
        str(host.kit),
    )

    assert result.returncode == 0, result.stderr
    commands = host.commands()
    [extract] = [c for c in commands if c.startswith("dpkg-deb -x ")]
    assert extract.split()[2].rsplit("/", 1)[1].startswith("homelab-daniel-box_")
    [early] = [c for c in commands if c.endswith("/usr/lib/sysusers.d/homelab.conf")]
    install = next(c for c in commands if c.startswith("apt-get install -y /"))
    assert commands.index(early) < commands.index(install)


def test_the_offline_local_build_times_each_phase_on_stderr(tmp_path):
    host = FakeHost(tmp_path, hostname="daniel-box")
    host.stock_kit()

    result = host.run(
        "daniel-box",
        "--local-build",
        str(host.clone(tmp_path)),
        "--offline",
        str(host.kit),
    )

    assert result.returncode == 0, result.stderr
    phases = re.findall(
        r"^bootstrap-host: (.+) took \d+s$", result.stderr, re.MULTILINE
    )
    assert phases == [
        "the nfpm and uv fetch",
        "the git archive of the clone",
        "the copy of the kit's caches",
        "the host package render",
        "the homelab-tools fetch",
        "the three nfpm builds",
        "the apt-get update",
        "the apt-get install",
    ]


def test_every_uv_run_of_the_local_build_skips_the_lint_group(tmp_path):
    host = FakeHost(tmp_path, hostname="daniel-box")
    host.stock_kit()

    result = host.run("daniel-box", "--local-build", str(host.clone(tmp_path)))

    assert result.returncode == 0, result.stderr
    calls = (host.root / "uv.calls").read_text().splitlines()
    assert len(calls) >= 3
    assert [c for c in calls if "run --frozen --no-group lint " not in c] == []
