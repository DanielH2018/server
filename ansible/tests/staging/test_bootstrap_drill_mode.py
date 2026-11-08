"""The drill orchestrator's bootstrap drill mode (`--rehearse-bootstrap`), run under bash against stubs.

The orchestrator half asserts the properties the mode promises on every run:

- the guest is destroyed and its disk deleted on EVERY exit path, and nothing is `virsh define`d;
- the monthly drill's evidence is untouched: no stamp and no textfile verdict;
- nothing is shipped into the guest until the egress fence has been measured, and then only the
  guest-side script and the mask list: no cluster token and no R2 credential.

`virsh`, `qemu-img`, `ssh`, `git`, `logger` and the repo's python are stubbed; the python stub
writes a small rendered tree in place of the real renderer. The guest-side half runs the guest
script itself with `id`, `systemctl`, `ufw`, `dpkg-query` and `k3s` stubbed.

Run: uv run pytest ansible/tests/staging/test_bootstrap_drill_mode.py
"""

import os
import subprocess
import tarfile

import pytest
from _drill_mode import (
    QEMU_IMG,
    VIRSH,
    Run,
    assert_cleaned_up,
    assert_drill_evidence_untouched,
)
from _fence_probe import (
    CHECK_LIB,
    ENTRYPOINT,
    HYPERVISOR,
    ROLE_CHECK_LIB,
    rendered_orchestrator,
)
from lib.proc_testing import fake_bin, path_with

GUEST_SCRIPT = HYPERVISOR / "files" / "bootstrap-drill-guest-run.sh"
SHA = "c0ffee" * 6 + "abcd"
# The guest-side subcommands, in the order the orchestrator runs them.
STEPS = ["prepare", "until4", "deviate", "bootstrap", "start", "check"]

# Stands in for the repo's `.venv/bin/python` running homelab_render.py: a host render with the
# four units the guest keeps, a drop-in, a template unit, a socket and wants links, and the
# homelab-updater package whose timer daniel-box's package enables.
RENDER = """\
#!/bin/bash
echo "render $2" >>"$EVENTS"
out="${@: -1}"
if [[ "$2" == host ]]; then
  u="$out/hosts/daniel-box/root/usr/lib/systemd/system"
  mkdir -p "$u/k3s.service.d" "$u/timers.target.wants" "$u/multi-user.target.wants"
  touch "$u/k3s.service.d/50-homelab.conf" "$u/homelab-reconcile.service" \\
    "$u/homelab-secrets.service" "$u/coredns-host.service" "$u/homelab-builder.service" \\
    "$u/homelab-builder.timer" "$u/homelab-rootq.socket" "$u/claude-triage@.service" \\
    "$u/timers.target.wants/homelab-builder.timer" "$u/timers.target.wants/homelab-updater.timer" \\
    "$u/multi-user.target.wants/coredns-host.service"
else
  u="$out/packages/homelab-updater/root/usr/lib/systemd/system"
  mkdir -p "$u" && touch "$u/homelab-updater.service" "$u/homelab-updater.timer"
fi
"""
MASKS = [
    "claude-triage@.service",
    "homelab-builder.service",
    "homelab-builder.timer",
    "homelab-rootq.socket",
    "homelab-updater.service",
    "homelab-updater.timer",
]
GIT = '#!/bin/bash\necho "$SHA"\n'
# Records each syslog line, so a test can read which tag the run logged under.
LOGGER = '#!/bin/bash\necho "logger $*" >>"$EVENTS"\n'
# Stands in for `ssh <guest> ...`. A detached step reports exit 1 through `rc` when it is FAIL_AT,
# and nothing at all when it is NEVER_DONE; a direct step exits 1 when it is FAIL_AT.
SSH = """\
#!/bin/bash
args="$*"
case "$args" in
  true) exit 0 ;;
  *"tee /usr/local/bin/bootstrap-drill-guest-run"*) cat >/dev/null; echo "ssh ship" >>"$EVENTS"; exit 0 ;;
  *"tar -C /var/tmp/bootstrap-drill"*) cat >"$T/staged.tar"; echo "ssh stage" >>"$EVENTS"; exit 0 ;;
esac
read -ra sub <<<"${args##*bootstrap-drill-guest-run }"
case "${sub[0]}" in
  detached) echo "step ${sub[*]:1}" >>"$EVENTS"; exit 0 ;;
  rc)
    [[ "${sub[1]}" == "$NEVER_DONE" ]] && exit 0
    [[ "${sub[1]}" == "$FAIL_AT" ]] && echo 1 || echo 0
    exit 0 ;;
  out) echo "${sub[1]}: last line"; exit 0 ;;
  state) echo "the guest's state"; exit 0 ;;
esac
echo "step ${sub[*]}" >>"$EVENTS"
[[ "${sub[0]}" == "$FAIL_AT" ]] && { echo "node daniel-box Ready: no answer"; exit 1; }
[[ "${sub[0]}" == check ]] && echo "node daniel-box Ready: True"
exit 0
"""

DRIVER = """\
#!/usr/bin/env bash
source "$ORCH" "$@"
export PATH="$STUBS:$PATH"
trap teardown EXIT
LOG_DIR="$T/evidence"; mkdir -p "$LOG_DIR"
NAME=etcd-drill DISK="$T/disk.qcow2" WORK="" STAMP_DIR="$T/stamp" LOCK="$T/lock"
XML="$T/xml" BASE_IMAGE="$T/base" SEED="$T/seed" GUEST_HOSTKEY_PUB="$T/hostkey.pub" IDENTITY="$T/id"
S3_ENV="$T/absent-s3.env" AGENT_ENV="$T/absent-agent.env"
BD_GUEST_SCRIPT="$GUEST_SCRIPT" BD_STEP_DEADLINE=1 BD_READY_DEADLINE=1 RH_POLL=0.1
for f in "$XML" "$BASE_IMAGE" "$SEED" "$GUEST_HOSTKEY_PUB" "$IDENTITY"; do echo x >"$f"; done
ssh_argv() { printf '%s\\n' "$STUBS/guest-ssh"; }
fence_check() { echo fence >>"$EVENTS"; [[ -z "$LEAK" ]] || fail "egress fence BROKEN: PRODVIP answered"; }
rehearse_bootstrap
"""


def run(tmp_path, *, fail_at="", never_done="", leak="", args=()):
    body = rendered_orchestrator()
    assert CHECK_LIB in body, "the harness's substitution is gone"
    assert body.rstrip().endswith(ENTRYPOINT)
    textfile = tmp_path / "textfile"
    textfile.mkdir()
    sourceable = (
        body.replace(CHECK_LIB, str(ROLE_CHECK_LIB)).rstrip().rsplit(ENTRYPOINT, 1)[0]
    )
    (tmp_path / "orch.sh").write_text(f'export TEXTFILE_DIR="{textfile}"\n{sourceable}')
    (tmp_path / "driver.sh").write_text(DRIVER)
    stubs = fake_bin(
        tmp_path / "bin",
        virsh=VIRSH,
        git=GIT,
        logger=LOGGER,
        **{"qemu-img": QEMU_IMG, "guest-ssh": SSH},
    )
    repo = tmp_path / "repo"
    fake_bin(repo / ".venv" / "bin", python=RENDER)
    result = Run(tmp_path)
    result.done = subprocess.run(
        [
            "bash",
            str(tmp_path / "driver.sh"),
            "--rehearse-bootstrap",
            "--repo",
            str(repo),
            *args,
        ],
        env={
            **os.environ,
            "ORCH": str(tmp_path / "orch.sh"),
            "STUBS": str(stubs),
            "T": str(tmp_path),
            "EVENTS": str(tmp_path / "events"),
            "GUEST_SCRIPT": str(GUEST_SCRIPT),
            "SHA": SHA,
            "FAIL_AT": fail_at,
            "NEVER_DONE": never_done,
            "LEAK": leak,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result


def test_a_passing_drill_runs_every_step_after_the_fence_and_touches_no_drill_evidence(
    tmp_path,
):
    r = run(tmp_path)
    assert r.done.returncode == 0, r.done.stdout + r.done.stderr
    assert r.steps == STEPS
    events = r.events
    assert events.index("render host") < events.index("fence")
    assert (
        events.index("fence")
        < events.index("ssh ship")
        < events.index("ssh stage")
        < events.index(f"step prepare {SHA}")
    )
    assert "step deviate 192.168.140.11 192.168.140.1" in events
    assert "Ready: True" in r.evidence("bootstrap-verdict.txt")
    logged = [e for e in events if e.startswith("logger ")]
    assert any("-t etcd-drill-bootstrap-drill status=up" in e for e in logged), logged
    assert not any("-t etcd-restore-drill-vm" in e for e in logged), logged
    assert_cleaned_up(r)
    assert_drill_evidence_untouched(r)
    with tarfile.open(r.t / "staged.tar") as staged:
        assert sorted(staged.getnames()) == [".", "./masks"]
        member = staged.extractfile("./masks")
        assert member is not None
        masks = member.read().decode().split()
    assert masks == MASKS


@pytest.mark.parametrize("fail_at", STEPS)
def test_a_failing_step_tears_the_guest_down_and_leaves_the_drill_evidence_alone(
    tmp_path, fail_at
):
    r = run(tmp_path, fail_at=fail_at)
    assert r.done.returncode == 1
    at = STEPS.index(fail_at)
    assert r.steps[: at + 1] == STEPS[: at + 1]
    assert not set(r.steps) & set(STEPS[at + 1 :])
    assert r.evidence("bootstrap-state.txt").strip() == "the guest's state"
    assert_cleaned_up(r)
    assert_drill_evidence_untouched(r)


def test_a_detached_step_that_never_finishes_fails_at_its_deadline_and_tears_down(
    tmp_path,
):
    r = run(tmp_path, never_done="bootstrap")
    assert r.done.returncode == 1
    assert "bootstrap did not finish in 1s" in r.done.stdout
    assert "start" not in r.steps
    assert_cleaned_up(r)
    assert_drill_evidence_untouched(r)


def test_a_fence_leak_ships_nothing_into_the_guest(tmp_path):
    r = run(tmp_path, leak="PRODVIP")
    assert r.done.returncode == 1
    assert "ssh ship" not in r.events and r.steps == []
    assert_cleaned_up(r)
    assert_drill_evidence_untouched(r)


def test_a_suc_rehearsal_flag_is_refused_before_a_guest_exists(tmp_path):
    r = run(tmp_path, args=("--expect", "upgrade"))
    assert r.done.returncode == 1
    assert "belong to --rehearse-suc" in r.done.stdout
    assert not any("create" in e for e in r.events)
    assert_drill_evidence_untouched(r)


# ── The guest-side script ────────────────────────────────────────────────────────────────────

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
}


def guest(tmp_path, *args, **env):
    stage = tmp_path / "stage"
    stage.mkdir(exist_ok=True)
    defaults = {"UNITS": "k3s.service", "UFW": "active", "MISSING": "", "READY": "True"}
    return subprocess.run(
        ["bash", str(GUEST_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": path_with(fake_bin(tmp_path / "bin", **GUEST_STUBS)),
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
