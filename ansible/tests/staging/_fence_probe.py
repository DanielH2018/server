#!/usr/bin/env python3
"""Render the drill orchestrator and run its egress-fence leg under bash against stub dials.

Used by test_staging_egress_fence.py. Not a test module itself.

The leg is exercised rather than pattern-matched because what breaks in it is shell logic and a
four-way verdict, neither of which a textual guard can see. Three things are stubbed: the ssh to
the guest, the dials on both sides of it, and `logger`. Every decision the leg makes runs
unmodified.
"""

import ipaddress
import os
import re
import subprocess

from lib import yaml_fast
from lib.proc_testing import fake_bin

from _helpers import ALL_VARS, HOST_VARS, ROLES
from _shell_render import render_shell_script

HYPERVISOR = ROLES / "setup" / "hypervisor"
ORCHESTRATOR = HYPERVISOR / "templates" / "etcd-restore-drill-vm.sh.j2"
# The orchestrator sources the check library (M6 batch B4) and writes a textfile verdict from
# `report` and `report_fence`. A runner has no /usr/local/lib copy, and daniel-box does, so the
# harness points the source at the role's copy and the textfile directory at tmp_path. Without
# the second, a run on daniel-box would write the real collector directory. The harness asserts
# on the literal first, the way _pi_health.py does, so a renamed library fails here instead of
# quietly disarming the run under test.
CHECK_LIB = "/usr/local/lib/homelab-check-lib.sh"
ROLE_CHECK_LIB = ROLES / "setup" / "initial_setup" / "files" / "homelab-check-lib.sh"
# The two lines the harness strips to make the orchestrator sourceable. Named so a rename fails
# loudly rather than leaving the harness running `main` against a real libvirt.
ENTRYPOINT = 'trap teardown EXIT\nmain "$@"'


def _all_vars():
    return yaml_fast.safe_load(ALL_VARS.read_text())


def _hypervisor_defaults():
    return yaml_fast.safe_load((HYPERVISOR / "defaults" / "main.yml").read_text())


def _load_host_vars(host: str):
    return yaml_fast.safe_load((HOST_VARS / f"{host}.yml").read_text()) or {}


# ── The reachability half: the orchestrator's fence leg, run against stub dials ─────────────
#
# The leg is exercised rather than pattern-matched because what breaks in it is shell logic and
# a four-way verdict, neither of which a textual guard can see. Three things are stubbed: the
# ssh to the guest, the dials on both sides of it, and `logger`. Every decision the leg makes
# runs unmodified.

# Stands in for `ssh <guest>`. The orchestrator hands it `bash -s` and pipes the dial script in,
# exactly as it does to the real guest. `$BASH` is an absolute path, so the guest's PATH can be
# narrowed to the stub directory alone — which is what lets a test model a guest with no `ping`.
# The dial stubs carry an absolute shebang for the same reason: `/usr/bin/env` is not on that PATH.
GUEST_SSH_STUB = """\
#!/bin/bash
export FENCE_SIDE=guest PATH="$GUEST_STUBS"
exec "$BASH" "${@:2}"
"""

# One dial stub for `curl` and `ping`. It answers from a list of reachable targets, one per line,
# chosen by which side of the fence it runs on — so a target can be reachable from daniel-server
# and refused from the guest, which is the whole point of the control leg. Builtins only: the
# guest's PATH holds nothing but these stubs.
DIAL_STUB = """\
#!/bin/bash
target="${@: -1}"
list="$HOST_REACHABLE"
[[ "${FENCE_SIDE:-host}" == guest ]] && list="$GUEST_REACHABLE"
while read -r line; do [[ "$line" == "$target" ]] && exit 0; done < "$list"
exit 7
"""

# `ip -4 neigh show dev cni0`, answering with one REACHABLE neighbour in the pod CIDR. The real
# one would report this host's own neighbours, which is not a fixture.
IP_STUB = """\
#!/bin/bash
[[ -n "$STUB_POD_IP" ]] && echo "$STUB_POD_IP dev cni0 lladdr aa:bb:cc:dd:ee:ff REACHABLE"
exit 0
"""

# `logger`, recording instead of writing. The fence leg's own reporting calls it, and those tags
# ship to Loki — a fixture verdict on the real syslog lands on a dashboard beside real ones.
# ansible/tests/leakguard.py fails the test that lets one through, and fence_reports() below reads
# the recording, which keeps this stub from failing open.
LOGGER_STUB = """\
#!/bin/bash
printf '%s\\n' "$*" >>"$LOGGER_CALLS"
"""

# Runs the leg and nothing else. `report` is the drill's own verdict; `fail` records the
# message the way the real one does before exiting non-zero.
DRIVER = """\
#!/usr/bin/env bash
source "$ORCH"
export PATH="$STUBS:$PATH"
LOG_DIR="$EVIDENCE"
NAME=etcd-drill
fail() { printf 'FAIL %s\\n' "$*" >"$VERDICT"; exit 1; }
report() { :; }
ssh_argv() { printf '%s\\n' "$STUBS/guest-ssh"; }
fence_check
printf 'VERDICT %s\\n' "$FENCE_VERDICT" >"$VERDICT"
"""

CONTROL = "https://1.1.1.1"
# The pod IP the `ip neigh` stub answers with, inside k3s_pod_cidr.
POD_IP = "10.42.1.216"


def host_reaches():
    """What daniel-server itself can reach: the two ALLOCATED targets.

    Their control leg is the only thing separating a stale address from a fence that holds, so a
    test that wants a clean hold has to say the host still reaches them.
    """
    return [
        _hypervisor_defaults()["hypervisor_etcd_drill_fence_longhorn_url"],
        POD_IP,
    ]


def rendered_orchestrator():
    """The orchestrator as the shared accessor renders it, `hostvars` repointed at three hosts.

    Everything else the template reads — the inventory, the role's own defaults — comes from
    the accessor's own context, which is what `validate/shell_templates.py` renders and lints
    (#3178). `hostvars` alone needs an override: the accessor's base stubs only one host
    (`daniel-server`), and two of the fence's dials are OTHER hosts' addresses
    (`hostvars['daniel-box'].server_ip`, `hostvars['daniel-pi'].server_ip`). Left at the
    accessor's default, either lookup renders the literal `STUB`, which no `DIAL_STUB` fixture
    ever lists as reachable — a dial that can never answer, which reads as a held fence no
    matter which way the leg under test should go.
    """
    return render_shell_script(
        "setup",
        "hypervisor",
        ORCHESTRATOR.name,
        overrides={
            "hostvars": {
                host: _load_host_vars(host)
                for host in ("daniel-box", "daniel-pi", "daniel-server")
            }
        },
    )


def fence_targets_block():
    """The body of `fence_targets()`, where every dialled address is written."""
    body = rendered_orchestrator()
    match = re.search(r"^fence_targets\(\) \{\n(.*?)^\}$", body, re.S | re.M)
    assert match, (
        f"no fence_targets() in the rendered {ORCHESTRATOR.name}. The reachability gate is "
        f"gone, and every shape test above still passes."
    )
    return match.group(1)


def dialled_addresses(block):
    """Every literal IPv4 address the leg dials, including the one inside FENCE_LONGHORN_URL."""
    found = [
        ipaddress.ip_address(address)
        for address in re.findall(r"fence_curl [a-z]+://(\d+\.\d+\.\d+\.\d+)", block)
    ]
    if "FENCE_LONGHORN_URL" in block:
        url = _hypervisor_defaults()["hypervisor_etcd_drill_fence_longhorn_url"]
        found += [
            ipaddress.ip_address(a) for a in re.findall(r"//(\d+\.\d+\.\d+\.\d+)", url)
        ]
    return found


def run_fence(
    tmp_path,
    guest_reachable=(),
    host_reachable=(),
    guest_tools=("curl", "ping"),
    pod_ip=POD_IP,
):
    """Render the orchestrator, run its fence leg alone, and return (verdict, evidence lines).

    The verdict is `VERDICT hold`, `VERDICT hold,unproven=<labels>`, or the `FAIL <message>` the
    leg exits with.
    """
    body = rendered_orchestrator()
    assert body.rstrip().endswith(ENTRYPOINT), (
        f"{ORCHESTRATOR.name} no longer ends with {ENTRYPOINT!r}. The harness strips those two "
        f"lines to source the script; without the strip it would run a real drill."
    )
    orch = tmp_path / "etcd-restore-drill-vm"
    assert CHECK_LIB in body, f"{ORCHESTRATOR.name} no longer sources {CHECK_LIB}"
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir()
    sourceable = (
        body.replace(CHECK_LIB, str(ROLE_CHECK_LIB)).rstrip().rsplit(ENTRYPOINT, 1)[0]
    )
    orch.write_text(f'export TEXTFILE_DIR="{textfile_dir}"\n{sourceable}')

    stubs = fake_bin(
        tmp_path / "bin",
        curl=DIAL_STUB,
        ping=DIAL_STUB,
        ip=IP_STUB,
        logger=LOGGER_STUB,
        **{"guest-ssh": GUEST_SSH_STUB},
    )
    # Only the tools named in `guest_tools` exist on the guest side, so a leg that dials from
    # the guest with a tool the guest lacks fails the way the VM would.
    guest_stubs = fake_bin(
        tmp_path / "guest-bin",
        **{name: DIAL_STUB for name in ("curl", "ping") if name in guest_tools},
    )

    guest_list, host_list = tmp_path / "guest-reachable", tmp_path / "host-reachable"
    guest_list.write_text("".join(f"{t}\n" for t in guest_reachable))
    host_list.write_text("".join(f"{t}\n" for t in host_reachable))
    evidence, verdict = tmp_path / "evidence", tmp_path / "verdict"
    evidence.mkdir()

    driver = tmp_path / "driver.sh"
    driver.write_text(DRIVER)
    done = subprocess.run(
        ["bash", str(driver)],
        env={
            **os.environ,
            "ORCH": str(orch),
            "STUBS": str(stubs),
            "GUEST_STUBS": str(guest_stubs),
            "GUEST_REACHABLE": str(guest_list),
            "HOST_REACHABLE": str(host_list),
            "EVIDENCE": str(evidence),
            "VERDICT": str(verdict),
            "STUB_POD_IP": pod_ip,
            "LOGGER_CALLS": str(tmp_path / "logger-calls"),
        },
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert verdict.exists(), (
        f"the fence leg neither reported a verdict nor called fail(). stdout: {done.stdout} "
        f"stderr: {done.stderr}"
    )
    fence_log = evidence / "egress-fence.log"
    lines = fence_log.read_text().splitlines() if fence_log.exists() else []
    return verdict.read_text().strip(), lines


def logger_calls(tmp_path):
    """Every `logger` invocation the leg made, as the argument string it passed."""
    calls = tmp_path / "logger-calls"
    return calls.read_text().splitlines() if calls.exists() else []


# `-t <tag> fence=<status> run=<id> <message>`, the line report_fence logs.
_FENCE_LINE = re.compile(r"^-t \S+ fence=(?P<status>\S+) run=\S+ (?P<message>.*)$")


def fence_reports(tmp_path):
    """Every fence verdict the leg reported, as `(status, message)` pairs, from its syslog lines.

    The line is the only place the verdict's message goes since M6 batch B4's Cutover-2 dropped
    the Kuma push. Reading it through the recording `logger` stub keeps that stub from failing
    open: a leg that started calling `/usr/bin/logger` by absolute path would leave this empty.
    """
    return [
        (m["status"], m["message"])
        for m in map(_FENCE_LINE.match, logger_calls(tmp_path))
        if m
    ]


def fence_textfile_success(tmp_path):
    """The fence check's `homelab_check_success` value from the run's textfile, or None."""
    prom = tmp_path / "textfile" / "etcd-restore-drill-egress-fence.prom"
    if not prom.exists():
        return None
    for line in prom.read_text().splitlines():
        if line.startswith("homelab_check_success{"):
            return line.rsplit(" ", 1)[1]
    return None


def state(lines, label):
    """The verdict the evidence file records for one target."""
    line = next((l for l in lines if l.startswith(label)), None)
    assert line, f"{label} is not in the evidence: {lines}"
    return line[len(label) :].strip()
