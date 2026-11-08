"""The guest-side half of the bootstrap drill, `bootstrap-drill-guest-run.sh`, run under bash.

The orchestrator half is test_bootstrap_drill_mode.py. Here the guest script itself runs with
`id`, `systemctl`, `ufw`, `dpkg-query`, `k3s`, `hostnamectl`, `cloud-init`, `apt-get` and `git`
stubbed, and with its stage and root directories under tmp_path.

Run: uv run pytest ansible/tests/staging/test_bootstrap_drill_guest_run.py
"""

import os
import subprocess
import time

import pytest
from _fence_probe import HYPERVISOR
from lib.proc_testing import fake_bin, path_with

GUEST_SCRIPT = HYPERVISOR / "files" / "bootstrap-drill-guest-run.sh"
SHA = "c0ffee" * 6 + "abcd"

# Holds the step open until the test creates $T/go, then exits 3.
BOOTSTRAP_HOST = """\
#!/bin/bash
echo "bootstrap-host.sh $*"
until [[ -e "$T/go" ]]; do sleep 0.05; done
exit 3
"""
GUEST_STUBS = {
    "id": "#!/bin/bash\necho 0\n",
    "systemctl": """\
#!/bin/bash
echo "systemctl $*" >>"$T/calls"
[[ "$1" == list-unit-files ]] && printf '%s enabled enabled\\n' $UNITS
exit 0
""",
    "ufw": """\
#!/bin/bash
echo "ufw $*" >>"$T/calls"
[[ "$1" == status ]] && echo "Status: $UFW"
exit 0
""",
    "dpkg-query": '#!/bin/bash\n[[ "${@: -1}" == "$MISSING" ]] || printf "install ok installed"\n',
    "k3s": '#!/bin/bash\nprintf "%s" "$READY"\n',
    "hostnamectl": '#!/bin/bash\necho "hostnamectl $*" >>"$T/calls"\n',
    "cloud-init": '#!/bin/bash\necho "cloud-init $*" >>"$T/calls"\n',
    "apt-get": '#!/bin/bash\necho "apt-get $*" >>"$T/calls"\n',
    # A clone holds the two files prepare copies out of it.
    "git": """\
#!/bin/bash
echo "git $*" >>"$T/calls"
if [[ "$1" == clone ]]; then
  mkdir -p "${@: -1}/hosts/keys"
  touch "${@: -1}/hosts/bootstrap-host.sh" "${@: -1}/hosts/keys/homelab-archive-keyring.gpg"
fi
exit 0
""",
}


def guest(tmp_path, *args, **env):
    stage = tmp_path / "stage"
    stage.mkdir(exist_ok=True)
    # Written once: rewriting a stub truncates it under a detached step that may be exec'ing it.
    stubs = tmp_path / "bin"
    if not stubs.is_dir():
        fake_bin(stubs, **GUEST_STUBS)
    defaults = {"UNITS": "k3s.service", "UFW": "active", "MISSING": "", "READY": "True"}
    return subprocess.run(
        ["bash", str(GUEST_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": path_with(stubs),
            "T": str(tmp_path),
            "BOOTSTRAP_DRILL_STAGE": str(stage),
            "BOOTSTRAP_DRILL_ROOT": str(tmp_path / "root"),
            **defaults,
            **env,
        },
    )


def test_deviate_writes_the_override_the_masks_and_the_one_ssh_rule(tmp_path):
    (tmp_path / "stage").mkdir()
    (tmp_path / "stage" / "masks").write_text(
        "homelab-builder.timer\nclaude-triage@.service\n"
    )
    done = guest(tmp_path, "deviate", "192.168.140.11", "192.168.140.1")
    assert done.returncode == 0, done.stderr
    root = tmp_path / "root"
    override = root / "etc/rancher/k3s/config.yaml.d/90-drill.yaml"
    assert override.read_text().splitlines() == [
        'node-ip: "192.168.140.11"',
        'advertise-address: "192.168.140.11"',
        "etcd-s3: false",
    ]
    runtime = root / "run/systemd/system"
    assert sorted(p.name for p in runtime.iterdir()) == [
        "claude-triage@.service",
        "homelab-builder.timer",
    ]
    assert all(os.readlink(p) == "/dev/null" for p in runtime.iterdir())
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls == [
        "systemctl daemon-reload",
        "ufw allow from 192.168.140.1 to any port 22 proto tcp",
    ]


def test_deviate_refuses_a_mask_line_that_is_not_a_unit_name(tmp_path):
    (tmp_path / "stage").mkdir()
    (tmp_path / "stage" / "masks").write_text("../../etc/passwd.service\n")
    done = guest(tmp_path, "deviate", "192.168.140.11", "192.168.140.1")
    assert done.returncode == 1
    assert "not a unit name" in done.stderr
    assert not (tmp_path / "root/etc/passwd.service").exists()


def test_check_passes_when_every_part_of_the_host_half_holds(tmp_path):
    done = guest(tmp_path, "check")
    assert done.returncode == 0, done.stdout
    assert "node daniel-box Ready: True" in done.stdout


@pytest.mark.parametrize(
    ("env", "line"),
    [
        ({"MISSING": "homelab-updater"}, "homelab-updater: not installed"),
        (
            {"UNITS": "k3s.service k3s-agent.service"},
            "k3s units: k3s.service k3s-agent.service",
        ),
        ({"READY": "False"}, "node daniel-box Ready: False"),
        ({"UFW": "inactive"}, "ufw: Status: inactive"),
    ],
    ids=["package-missing", "second-k3s-unit", "node-not-ready", "ufw-inactive"],
)
def test_check_fails_and_names_the_part_that_does_not_hold(tmp_path, env, line):
    done = guest(tmp_path, "check", **env)
    assert done.returncode == 1
    assert line in done.stdout


def test_prepare_names_the_guest_daniel_box_and_clones_at_the_sha(tmp_path):
    (tmp_path / "root" / "etc").mkdir(parents=True)
    hosts = tmp_path / "root" / "etc" / "hosts"
    hosts.write_text("127.0.0.1 localhost\n")
    for _ in range(2):
        done = guest(tmp_path, "prepare", SHA)
        assert done.returncode == 0, done.stderr
    assert hosts.read_text().splitlines() == [
        "127.0.0.1 localhost",
        "127.0.1.1 daniel-box",
    ]
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls[0] == "hostnamectl set-hostname daniel-box"
    stop = next(
        i for i, c in enumerate(calls) if c.startswith("systemctl stop apt-daily")
    )
    assert stop < calls.index("apt-get update")
    clone = tmp_path / "stage" / "clone"
    assert f"git -C {clone} checkout --quiet --detach {SHA}" in calls
    run_dir = tmp_path / "stage" / "run"
    assert sorted(p.name for p in run_dir.iterdir()) == [
        "bootstrap-host.sh",
        "homelab-archive-keyring.gpg",
    ]


def test_a_detached_step_reports_no_code_while_it_runs_then_its_code_and_output(
    tmp_path,
):
    fake_bin(tmp_path / "stage" / "run", **{"bootstrap-host.sh": BOOTSTRAP_HOST})
    started = guest(tmp_path, "detached", "until4")
    assert started.returncode == 0, started.stderr
    assert guest(tmp_path, "rc", "until4").stdout == ""
    (tmp_path / "go").touch()
    deadline = time.monotonic() + 20
    while not (rc := guest(tmp_path, "rc", "until4").stdout.strip()):
        assert time.monotonic() < deadline, (
            "the detached step never wrote its exit code"
        )
        time.sleep(0.1)
    # Read the file, not `out`: whatever failed the step can fail the guest script again.
    out = tmp_path / "stage" / "until4.out"
    assert rc == "3", (
        f"until4 exited {rc}; until4.out:\n"
        f"{out.read_text() if out.exists() else '<missing>'}"
    )
    clone = tmp_path / "stage" / "clone"
    assert guest(tmp_path, "out", "until4").stdout.strip() == (
        f"bootstrap-host.sh daniel-box --until 4 --local-build {clone}"
    )
    assert guest(tmp_path, "detached", "check").returncode == 1
