#!/usr/bin/env python3
"""`deploy_lib/detach_notify.py` quotes the failed read when `probe.py health` exits 75 (#4444).

On `PROBE_UNREADABLE` the probe's stdout holds only its `PROBE-VERDICT:` line, and the read
that failed is on stderr. A detail taken from the first stdout line would post "unreadable" to
Discord instead of the ssh or curl error that says what to fix.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_detach_notify_unreadable.py
"""

import types

from deploy_tools.deploy_lib import detach_notify as notify_mod
from lib.exit_codes import PROBE_UNREADABLE

SSH_ERROR = "ssh daniel-pi docker inspect wg-easy failed: Connection refused"


def test_check_one_quotes_the_failed_read_when_the_probe_could_not_read():
    def run(argv, **kwargs):
        return types.SimpleNamespace(
            returncode=PROBE_UNREADABLE,
            stdout="PROBE-VERDICT: unreadable (probe.py health, exit 75)\n",
            # `uv run` can print its own warning ahead of the probe's message.
            stderr=f"warning: a uv notice\n{SSH_ERROR}\n",
        )

    state, detail = notify_mod.check_one(
        "wg-easy", tools=notify_mod.NotifyTools(run=run), platforms={"docker"}
    )
    # Not `unhealthy`: the read failed, so the container was never graded (#5543).
    assert state == "unreadable"
    assert detail == SSH_ERROR


def _probe_by_tag(codes):
    """A probe run whose exit code is `codes[tag]`, the tag being probe.py's argv[5]."""

    def run(argv, **kwargs):
        code = codes[argv[5]]
        return types.SimpleNamespace(
            returncode=code,
            stdout=f"{argv[5]}: exit {code}\n",
            stderr="no available server\n",
        )

    return notify_mod.NotifyTools(run=run, tag_platforms=lambda tag, host_vars: {"k8s"})


def test_a_gate_with_only_an_unreadable_tag_is_unsettled_and_unreadable():
    result = notify_mod.gate(
        ["sonarr", "radarr"],
        True,
        tools=_probe_by_tag({"sonarr": 0, "radarr": PROBE_UNREADABLE}),
    )
    assert (result.settled, result.unreadable) == (False, True)
    assert any(line.startswith("radarr: not graded") for line in result.lines)


def test_an_unhealthy_tag_beside_an_unreadable_one_is_not_unreadable():
    """One workload already failed, so the landing is unhealthy whatever the other reads."""
    result = notify_mod.gate(
        ["sonarr", "radarr"],
        True,
        tools=_probe_by_tag({"sonarr": 1, "radarr": PROBE_UNREADABLE}),
    )
    assert (result.settled, result.unreadable) == (False, False)


def test_detach_reports_an_unreadable_gate_as_ungraded_not_failed(capsys):
    code = notify_mod.main(
        ["--status", "0", "--log", "/tmp/x.log", "--tags", "radarr", "--no-post"],
        tools=_probe_by_tag({"radarr": PROBE_UNREADABLE}),
    )
    assert code == 1
    assert "deploy --detach UNGRADED (ansible exit 0)" in capsys.readouterr().out
