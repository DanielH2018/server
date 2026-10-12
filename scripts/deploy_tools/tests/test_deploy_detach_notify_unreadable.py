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
    assert state == "unhealthy"
    assert detail == SSH_ERROR
