"""netpol-baseline's one probe Job and its readiness gate must read the same target table.

Most of the probe's legs are inverted: a refused connection is the PASS case. So a target that
is down reads exactly like a target that is fenced, and the readiness gate in tasks/main.yml is
what tells them apart. The gate and the Job both read `netpol_baseline_probe_active`. These tests
pin that agreement, the lever that picks each plane's rows, and the clocks that bound the Job.

WHY THE QUERY SHAPE IS A TEST AND NOT A COMMENT. A Service with no ready pods keeps its
EndpointSlice and sets `endpoints: null`, not `[]`. kubectl's jsonpath filter cannot filter nil
— it exits 1 with `<nil> is not array or slice` — so the READ failed before the assert could
name the target. The filter form exits 1 on terraria (0 replicas) and 0 on freshrss; the `[*]`
form exits 0 on both. A failed read reports a Go template dump instead of "terraria is down".
"""

import re
import subprocess

from lib import yaml_fast
from _helpers import ALL_VARS, K8S_ROLES
from _k8s_render import render_role_template

ROLE = K8S_ROLES / "netpol-baseline"
TASKS = (ROLE / "tasks" / "main.yml").read_text()
# The comments explain the query shape they forbid, so a textual guard over the raw file would
# match its own documentation. Check the executable lines only.
TASK_CODE = "\n".join(
    line for line in TASKS.splitlines() if not line.lstrip().startswith("#")
)
PROBE_TEMPLATE = "netpol-probe-job.yaml.j2"
TARGETS = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())[
    "netpol_baseline_probe_targets"
]
OBS_NAMESPACE = yaml_fast.safe_load(ALL_VARS.read_text())["k8s_observability_namespace"]

# traefik is the control leg: open to every source by design, so it needs no gate.
_UNGATED_BY_DESIGN = {"traefik"}
_DIAL = re.compile(r"nc -w (\d+) -z (\S+) (\d+)")


def _job(enforced: bool = True, obs_enforced: bool = True) -> dict:
    text = render_role_template(
        "netpol-baseline",
        PROBE_TEMPLATE,
        {
            "netpol_baseline_enforced": enforced,
            "netpol_baseline_obs_enforced": obs_enforced,
        },
    )
    return yaml_fast.safe_load(text)


def _script(job: dict) -> str:
    return job["spec"]["template"]["spec"]["containers"][0]["command"][2]


def _dialled_services(script: str) -> set[str]:
    """Every Service the script opens a TCP connection to, namespace suffix stripped."""
    return {host.split(".")[0] for _w, host, _p in _DIAL.findall(script)}


def _ungated_dials(script: str, rows: list[dict]) -> set[str]:
    return _dialled_services(script) - {r["service"] for r in rows} - _UNGATED_BY_DESIGN


def test_the_readiness_query_does_not_filter_on_a_field_that_can_be_null() -> None:
    """The accepting half: the gate reads `endpoints[*]`, which survives `endpoints: null`."""
    assert "endpoints[*].conditions.ready" in TASK_CODE, (
        "the probe's readiness query no longer selects endpoints[*].conditions.ready"
    )


def test_a_jsonpath_filter_on_endpoints_is_rejected() -> None:
    """The rejecting half: the pre-fix query shape, verbatim, must not be back.

    A guard that only asserts the good shape is present would pass with both forms in the file.
    """
    assert "endpoints[?(" not in TASK_CODE, (
        "a jsonpath filter over `endpoints` is back in the probe's gate. It exits 1 with "
        "`<nil> is not array or slice` whenever a target has no ready pods, which kills the "
        "read before the assert can name the target."
    )


def test_the_gate_requires_an_explicit_ready_true() -> None:
    """Readiness is asserted, not merely presence: a target whose only endpoint is `false`
    must fail the gate."""
    assert "'true' in item.stdout.split()" in TASK_CODE, (
        "the probe's readiness assert no longer requires an explicit ready=true"
    )


def test_the_gate_loops_the_table_the_job_renders_from() -> None:
    assert 'loop: "{{ netpol_baseline_probe_active }}"' in TASK_CODE, (
        "the readiness gate no longer loops netpol_baseline_probe_active, the list the probe "
        "Job renders its legs from"
    )


def test_every_dialled_target_is_gated() -> None:
    """A leg hand-added to the template, outside the table, would run against a target nobody
    proved is up — a silent green for an inverted leg."""
    script = _script(_job())
    active = [r for r in TARGETS]
    assert not _ungated_dials(script, active), (
        f"the probe dials {sorted(_ungated_dials(script, active))}, which no table row gates. "
        "Add the target as a row of netpol_baseline_probe_targets instead of a hand-written leg."
    )
    # The check above can go red: an extra leg outside the table is found.
    assert _ungated_dials(script + "\nnc -w 5 -z sonarr-two 1\n", active) == {
        "sonarr-two"
    }


def test_every_expecting_row_renders_a_leg() -> None:
    """The opposite direction: a row with an `expect` that the template drops is a fence no
    leg tests."""
    dialled = _dialled_services(_script(_job()))
    expecting = {r["service"] for r in TARGETS if "expect" in r}
    assert expecting <= dialled, (
        f"rows with no rendered leg: {sorted(expecting - dialled)}"
    )


def test_each_lever_renders_only_its_own_plane() -> None:
    """The observability rows run under netpol_baseline_obs_enforced, the rest under
    netpol_baseline_enforced. Rendered every way round, so a row on the wrong lever fails."""
    homelab = {
        r["service"] for r in TARGETS if r["plane"] == "homelab" and "expect" in r
    }
    obs = {
        r["service"] for r in TARGETS if r["plane"] == "observability" and "expect" in r
    }
    assert homelab and obs

    only_obs = _dialled_services(_script(_job(enforced=False, obs_enforced=True)))
    assert not homelab & only_obs, (
        f"homelab rows ran with its lever off: {homelab & only_obs}"
    )
    assert obs <= only_obs

    only_homelab = _script(_job(enforced=True, obs_enforced=False))
    assert not obs & _dialled_services(only_homelab)
    assert f".{OBS_NAMESPACE} " not in only_homelab
    assert homelab <= _dialled_services(only_homelab)


def test_every_lever_combination_renders_valid_sh() -> None:
    for enforced, obs in [(True, True), (True, False), (False, True)]:
        result = subprocess.run(
            ["sh", "-n"],
            input=_script(_job(enforced, obs)),
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, f"enforced={enforced} obs={obs}: {result.stderr}"


def test_the_clocks_are_ordered() -> None:
    """script worst case < Job activeDeadlineSeconds < the Ansible wait.

    The worst case is under DROP rather than REJECT, where every `nc` runs to its `-w`. A
    deadline below it kills a probe that was still legitimately working; a wait below the
    deadline turns a real failure into an opaque wait timeout instead of the probe's message.
    """
    job = _job()
    script = _script(job)
    attempts = re.search(r"-ge (\d+) \]", script)
    sleep = re.search(r"sleep (\d+)", script)
    sentinel = re.search(r"until nc -w (\d+) -z speedtest", script)
    assert attempts and sleep and sentinel, "the sentinel control no longer retries"
    retry = int(attempts.group(1)) * (int(sentinel.group(1)) + int(sleep.group(1)))
    others = sum(int(w) for w, host, _p in _DIAL.findall(script) if host != "speedtest")
    wget = sum(int(t) for t in re.findall(r"wget -q -T (\d+)", script))
    worst = retry + others + wget

    deadline = job["spec"]["activeDeadlineSeconds"]
    wait = re.search(r"job/netpol-baseline-probe --timeout=(\d+)s", TASKS)
    assert wait, (
        "no `kubectl wait ... job/netpol-baseline-probe --timeout=<n>s` task found"
    )
    assert worst < deadline < int(wait.group(1)), (worst, deadline, int(wait.group(1)))


def test_terraria_is_absent_while_it_is_scaled_to_zero() -> None:
    """terraria runs at replicas 0, so gating it on a ready endpoint can only fail.

    One direction only. A count above 0 does not require the row: its `expect: open` leg dials
    :7777, which can crash a 1.4.5.7 server (#822), so the row comes back with the image fix
    rather than with the replica count. defaults/main.yml records the decision (#3848).
    """
    # In inventory, not terraria's defaults — k8s/game-stats renders the exporter
    # at the same count and a role default does not cross a role boundary.
    all_vars = yaml_fast.safe_load(ALL_VARS.read_text())
    services = {r["service"] for r in TARGETS}
    if int(all_vars["terraria_k8s_replicas"]) == 0:
        assert "terraria" not in services, (
            "terraria is scaled to zero but is still a probe target, so every full deploy.yml "
            "fails its readiness gate"
        )
