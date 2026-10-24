"""The drill orchestrator's SUC rehearsal mode (`--rehearse-suc`, H6.1b2), run under bash against stubs.

Three properties are asserted, because a rehearsal that fails any of them is worse than none:

- the guest is destroyed and its disk deleted on EVERY exit path, and nothing is ever `virsh define`d;
- the monthly drill's evidence is untouched: no stamp, no Kuma push, no textfile verdict;
- nothing is staged into the guest until the egress fence has been measured, and then only the
  bundle: no cluster token and no R2 credential.

`virsh`, `qemu-img`, `ssh` and the Kuma helper are stubbed. The helper (`suc_rehearsal.py`) and the
orchestrator's own control flow run unmodified. The rendered tree and the snapshots are fixtures
(ansible/tests/_suc_bundle.py), because the `system-upgrade` role lands in a parallel PR.

Run: uv run pytest ansible/tests/staging/test_suc_rehearsal_mode.py
"""

import json
import os
import subprocess
import tarfile

import pytest
from _fence_probe import (
    CHECK_LIB,
    ENTRYPOINT,
    HYPERVISOR,
    KUMA_LIB,
    ROLE_CHECK_LIB,
    _hypervisor_defaults,
    rendered_orchestrator,
)
from _helpers import REPO
from _suc_bundle import (
    DIGEST,
    TARGET,
    plan_without,
    red_snapshot,
    snapshot,
    write_rendered,
)
from lib.proc_testing import fake_bin, path_with

FILES = HYPERVISOR / "files"
FULL_TOKEN = "e" * 32
FENCE_TOKEN = "f" * 32
# The guest-side subcommands, in the order the orchestrator runs them.
STEPS = ["k3s", "flux-cli", "base", "publish"]

VIRSH = """\
#!/bin/bash
echo "virsh $*" >>"$EVENTS"
case "$3" in
  domstate) [[ -e "$T/running" ]] && echo running || exit 1 ;;
  create) touch "$T/running" ;;
  destroy) rm -f "$T/running" ;;
esac
exit 0
"""
QEMU_IMG = """\
#!/bin/bash
[[ "$1" == convert ]] && touch "${@: -1}"
exit 0
"""
# Stands in for `ssh <guest> ...`. The orchestrator hands it the guest-side subcommand after
# `sudo /usr/local/bin/suc-rehearsal-guest-run`, or a shell snippet that ships a file or a tar.
SSH = """\
#!/bin/bash
args="$*"
sub=""
[[ "$args" == *suc-rehearsal-guest-run\\ * ]] && sub="${args##*suc-rehearsal-guest-run }" && sub="${sub%% *}"
case "$args" in
  true) exit 0 ;;
  *"tee /usr/local/bin/suc-rehearsal-guest-run"*) cat >/dev/null; echo "ssh ship" >>"$EVENTS"; exit 0 ;;
  *"tar -C /var/tmp/suc-rehearsal"*) cat >"$T/staged.tar"; echo "ssh stage" >>"$EVENTS"; exit 0 ;;
esac
echo "ssh $args" >>"$EVENTS"
echo "step $sub" >>"$EVENTS"
[[ "$sub" == "$FAIL_AT" ]] && exit 1
case "$sub" in
  publish) echo "digest=$DIGEST" ;;
  snapshot) cat "$SNAPSHOT" ;;
  state) echo "the guest's state" ;;
esac
exit 0
"""
LOGGER = "#!/bin/bash\nexit 0\n"
KUMA_STUB = 'kuma_push() { printf "%s %s %s\\n" "$1" "$3" "$2" >>"$PUSHES"; }\n'

DRIVER = """\
#!/usr/bin/env bash
source "$ORCH" "$@"
export PATH="$STUBS:$PATH"
trap teardown EXIT
LOG_DIR="$T/evidence"; mkdir -p "$LOG_DIR"
NAME=etcd-drill DISK="$T/disk.qcow2" WORK="" STAMP_DIR="$T/stamp" LOCK="$T/lock"
XML="$T/xml" BASE_IMAGE="$T/base" SEED="$T/seed" GUEST_HOSTKEY_PUB="$T/hostkey.pub" IDENTITY="$T/id"
S3_ENV="$T/absent-s3.env" AGENT_ENV="$T/absent-agent.env"
SUC_HELPER="$HELPER" SUC_GUEST_SCRIPT="$GUEST_SCRIPT" RH_DEADLINE="$TEST_DEADLINE" RH_POLL=0.1
for f in "$XML" "$BASE_IMAGE" "$SEED" "$GUEST_HOSTKEY_PUB" "$IDENTITY"; do echo x >"$f"; done
echo "ssh-ed25519 AAAA key" >"$GUEST_HOSTKEY_PUB"
ssh_argv() { printf '%s\\n' "$STUBS/guest-ssh"; }
fence_check() { echo fence >>"$EVENTS"; [[ -z "$LEAK" ]] || fail "egress fence BROKEN: PRODVIP answered"; }
if [[ -n "$PROBE_ONLY" ]]; then
  echo "REHEARSE=$REHEARSE PUSH_TOKEN=$PUSH_TOKEN FENCE_PUSH_TOKEN=$FENCE_PUSH_TOKEN TAG=$TAG"
  exit 0
fi
rehearse_suc
"""


class Run:
    """One orchestrator run: its exit status and everything the stubs recorded."""

    def __init__(self, tmp_path):
        self.t = tmp_path
        self.done = None

    @property
    def events(self):
        path = self.t / "events"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def steps(self):
        return [e.split()[1] for e in self.events if e.startswith("step ")]

    def evidence(self, name):
        return (self.t / "evidence" / name).read_text()


def run(
    tmp_path,
    *,
    expect="upgrade",
    guest_version="v1.0.0+k3s1",
    fail_at="",
    leak="",
    snap=None,
    tree=None,
    deadline=1,
    probe_only="",
    extra_args=(),
):
    body = rendered_orchestrator()
    kuma_env = _hypervisor_defaults()["hypervisor_etcd_drill_kuma_env"]
    assert KUMA_LIB in body and CHECK_LIB in body and kuma_env in body, (
        "the harness's substitutions are gone"
    )
    assert body.rstrip().endswith(ENTRYPOINT)
    (tmp_path / "kuma-lib.sh").write_text(KUMA_STUB)
    # Both tokens are seeded, so a rehearsal that forgot to blank them WOULD push to the tiles.
    (tmp_path / "kuma.env").write_text(
        f"ETCD_DRILL_FULL_PUSH_TOKEN={FULL_TOKEN}\nETCD_DRILL_FENCE_PUSH_TOKEN={FENCE_TOKEN}\n"
    )
    textfile = tmp_path / "textfile"
    textfile.mkdir()
    sourceable = (
        body.replace(KUMA_LIB, str(tmp_path / "kuma-lib.sh"))
        .replace(CHECK_LIB, str(ROLE_CHECK_LIB))
        .replace(kuma_env, str(tmp_path / "kuma.env"))
        .rstrip()
        .rsplit(ENTRYPOINT, 1)[0]
    )
    (tmp_path / "orch.sh").write_text(f'export TEXTFILE_DIR="{textfile}"\n{sourceable}')
    (tmp_path / "driver.sh").write_text(DRIVER)
    (tmp_path / "snapshot.json").write_text(json.dumps(snap or snapshot()))
    stubs = fake_bin(
        tmp_path / "bin",
        virsh=VIRSH,
        **{"qemu-img": QEMU_IMG, "guest-ssh": SSH},
        logger=LOGGER,
    )
    rendered = write_rendered(tmp_path / "rendered", **(tree or {}))
    args = [
        "--rehearse-suc",
        "--repo",
        str(REPO),
        "--rendered",
        str(rendered),
        "--expect",
        expect,
    ]
    if guest_version:
        args += ["--guest-version", guest_version]
    result = Run(tmp_path)
    result.done = subprocess.run(
        ["bash", str(tmp_path / "driver.sh"), *args, *extra_args],
        env={
            **os.environ,
            "ORCH": str(tmp_path / "orch.sh"),
            "STUBS": str(stubs),
            "T": str(tmp_path),
            "EVENTS": str(tmp_path / "events"),
            "PUSHES": str(tmp_path / "pushes"),
            "HELPER": str(FILES / "suc_rehearsal.py"),
            "GUEST_SCRIPT": str(FILES / "suc-rehearsal-guest-run.sh"),
            "TEST_DEADLINE": str(deadline),
            "FAIL_AT": fail_at,
            "LEAK": leak,
            "SNAPSHOT": str(tmp_path / "snapshot.json"),
            "DIGEST": DIGEST,
            "PROBE_ONLY": probe_only,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result


def assert_cleaned_up(r):
    """The guest is gone, its disk is deleted, nothing was defined: the exit-path guarantee."""
    assert not (r.t / "disk.qcow2").exists(), "the guest's disk outlived the run"
    assert not (r.t / "running").exists(), "the guest was left running"
    created = any(" create " in e for e in r.events)
    destroyed = any(" destroy " in e for e in r.events)
    assert destroyed or not created, "a created guest was never destroyed"
    assert not any(" define " in e for e in r.events), "the rehearsal defined a domain"


def assert_drill_evidence_untouched(r):
    assert not (r.t / "stamp").exists(), "a rehearsal wrote the monthly drill's stamp"
    assert not (r.t / "pushes").exists(), "a rehearsal pushed to a Kuma tile"
    assert not list((r.t / "textfile").iterdir()), (
        "a rehearsal wrote a textfile verdict"
    )


@pytest.mark.parametrize(
    ("expect", "snap"),
    [("upgrade", snapshot()), ("red", red_snapshot())],
)
def test_a_passing_rehearsal_runs_every_step_after_the_fence_and_touches_no_drill_evidence(
    tmp_path, expect, snap
):
    r = run(tmp_path, expect=expect, snap=snap)
    assert r.done.returncode == 0, r.done.stdout + r.done.stderr
    assert r.steps[:4] == STEPS
    events = r.events
    assert (
        events.index("fence")
        < events.index("ssh ship")
        < events.index("ssh stage")
        < events.index("step k3s")
    )
    assert "PASS" in r.evidence("rehearsal-verdict.txt")
    assert_cleaned_up(r)
    assert_drill_evidence_untouched(r)
    with tarfile.open(r.t / "staged.tar") as staged:
        names = staged.getnames()
    assert all(n.startswith(("./bundle", "./hand")) or n == "." for n in names), names
    assert not [n for n in names if "token" in n or "s3" in n], names


@pytest.mark.parametrize("fail_at", STEPS)
def test_a_failing_step_tears_the_guest_down_and_leaves_the_drill_evidence_alone(
    tmp_path, fail_at
):
    r = run(tmp_path, fail_at=fail_at)
    assert r.done.returncode == 1
    assert r.steps[: STEPS.index(fail_at) + 1] == STEPS[: STEPS.index(fail_at) + 1]
    assert "snapshot" not in r.steps
    assert r.evidence("rehearsal-state.txt").strip() == "the guest's state"
    assert_cleaned_up(r)
    assert_drill_evidence_untouched(r)


def test_a_fence_leak_stages_nothing_into_the_guest(tmp_path):
    r = run(tmp_path, leak="PRODVIP")
    assert r.done.returncode == 1
    assert "ssh ship" not in r.events and r.steps == []
    assert_cleaned_up(r)
    assert_drill_evidence_untouched(r)


def test_a_verdict_that_never_arrives_fails_after_the_deadline_and_tears_down(tmp_path):
    r = run(tmp_path, snap=snapshot(label="old"), deadline=2)
    assert r.done.returncode == 1
    assert r.steps.count("snapshot") >= 2
    assert "WAIT" in r.evidence("rehearsal-verdict.txt")
    assert_cleaned_up(r)
    assert_drill_evidence_untouched(r)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"tree": {"plan": plan_without("nodeSelector")}},
            "preparing the bundle failed",
        ),
        ({"guest_version": TARGET}, "no upgrade could happen"),
        ({"expect": "sideways"}, "--expect must be upgrade or red"),
    ],
    ids=["unusable-bundle", "guest-already-at-the-target", "unknown-expectation"],
)
def test_a_rehearsal_that_could_prove_nothing_is_refused_before_a_guest_exists(
    tmp_path, kwargs, message
):
    r = run(tmp_path, **kwargs)
    assert r.done.returncode == 1 and message in r.done.stdout
    assert not any("create" in e for e in r.events)
    assert_cleaned_up(r)
    assert_drill_evidence_untouched(r)


def sourced(r, *args):
    """What the orchestrator's top level leaves set after `source orch.sh ARGS`."""
    return subprocess.run(
        [
            "bash",
            "-c",
            'f=$1; shift; source "$f" "$@"; echo "$REHEARSE $PUSH_TOKEN $FENCE_PUSH_TOKEN $TAG"',
            "_",
            str(r.t / "orch.sh"),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_without_the_flag_the_orchestrator_is_the_drill_and_with_it_both_tiles_are_blanked(
    tmp_path,
):
    r = run(tmp_path, probe_only="1")
    drill = sourced(r)
    assert drill.stdout.split() == [
        "0",
        FULL_TOKEN,
        FENCE_TOKEN,
        "etcd-restore-drill-vm",
    ], drill.stderr
    rehearsal = sourced(r, "--rehearse-suc")
    assert rehearsal.stdout.split() == ["1", "etcd-drill-suc-rehearsal"], (
        rehearsal.stderr
    )
    assert "unknown argument" in sourced(r, "--bogus").stderr


# The k3s installer returns before the kubelet registers the Node, and `kubectl wait --all` exits
# at once with "no matching resources found" while there is none (both 2026-10-24 rehearsals).
# This `k3s` stub reports no node for the first NODE_AFTER `get nodes` calls, then one, and its
# `wait` fails at once while there is none.
GUEST_STUBS = {
    "id": "#!/bin/bash\necho 0\n",
    "curl": '#!/bin/bash\nwhile [[ $# -gt 1 ]]; do [[ $1 == -o ]] && echo "#!/bin/sh" >"$2"; shift; done\n',
    # Reads its stdin: `echo | sha256sum` under pipefail fails on SIGPIPE if the stub exits first.
    "sha256sum": "#!/bin/bash\ncat >/dev/null\n",
    "sh": "#!/bin/bash\nexit 0\n",
    "k3s": """\
#!/bin/bash
echo "k3s $*" >>"$T/calls"
case "$2" in
  get)
    n=$(( $(cat "$T/gets" 2>/dev/null || echo 0) + 1 )); echo "$n" >"$T/gets"
    (( n > NODE_AFTER )) && echo node/guest ;;
  wait)  # as the real one: no Node yet is an immediate error, not a wait
    if (( $(cat "$T/gets" 2>/dev/null || echo 0) <= NODE_AFTER )); then
      echo "error: no matching resources found" >&2; exit 1
    fi
    [[ -n "$READY_FAILS" ]] && exit 1 ;;
esac
exit 0
""",
}


def guest_k3s(tmp_path, node_after, budget="30", ready_fails=""):
    env = {
        "T": str(tmp_path),
        "NODE_AFTER": str(node_after),
        "READY_FAILS": ready_fails,
        "SUC_NODE_WAIT_S": budget,
        "SUC_POLL_S": "0.05",
    }
    env["PATH"] = path_with(fake_bin(tmp_path / "bin", **GUEST_STUBS))
    return subprocess.run(
        [
            "bash",
            str(FILES / "suc-rehearsal-guest-run.sh"),
            "k3s",
            "v1.0.0+k3s1",
            "file:///installer",
            "0" * 64,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def test_the_node_wait_polls_until_the_node_registers_then_waits_for_ready(tmp_path):
    done = guest_k3s(tmp_path, node_after=3)
    assert done.returncode == 0, done.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert [c.split()[2] for c in calls] == ["get"] * 4 + ["wait"]


def test_a_node_that_never_registers_fails_with_its_own_message_and_never_waits(
    tmp_path,
):
    done = guest_k3s(tmp_path, node_after=10**9, budget="1")
    assert done.returncode == 1
    assert "no node registered within 1s" in done.stderr
    assert "never read Ready" not in done.stderr
    assert "wait" not in (tmp_path / "calls").read_text()


def test_a_registered_node_that_never_reads_ready_keeps_the_ready_message(tmp_path):
    done = guest_k3s(tmp_path, node_after=0, ready_fails="1")
    assert done.returncode == 1
    assert "the node never read Ready" in done.stderr
