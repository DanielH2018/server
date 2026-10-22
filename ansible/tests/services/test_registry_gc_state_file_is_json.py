"""registry-gc's state file is JSON whatever its message holds.

`finish()` writes `/var/lib/registry-gc/state.json` from `MSG`, which carries crane and kubectl
error text. An operator reads the file with `jq` after a run, so a message that breaks the JSON
breaks that read on the run whose message mattered (#4606).

`finish()` is sliced out of the RENDERED script and run with `curl`, `logger` and `kuma_push`
stubbed as shell functions. The `curl` stub is load-bearing: `finish()` sources
`/etc/healthchecks/ping.env` whenever it is readable, so on daniel-box an unstubbed run would
send a real `/fail` ping to the live `registry-gc` dead-man.
"""

import json
import os
import shlex
from pathlib import Path

from _shell_render import rendered_shell_function
from lib.proc_testing import run

FINISH = rendered_shell_function(
    "setup", "k8s_host_files", "registry-gc.sh.j2", "finish"
)


def _finish(tmp_path: Path, status: str, msg: str) -> dict:
    """Run finish() once with `status` and `msg`, and return the state file parsed as JSON."""
    state = tmp_path / "state" / "state.json"
    script = f"""
STATE_DIR={shlex.quote(str(state.parent))}
STATE_FILE={shlex.quote(str(state))}
PUSH_TOKEN=
STATUS={shlex.quote(status)}
curl() {{ :; }}
logger() {{ :; }}
kuma_push() {{ :; }}
{FINISH}
finish
"""
    # MSG travels in the environment rather than in the script text, so the newline and the
    # quotes reach bash exactly as crane or kubectl would have produced them.
    run(["bash", "-c", script], env={**os.environ, "MSG": msg}, check=True)
    return json.loads(state.read_text())


def test_a_message_with_a_backslash_a_quote_and_a_newline_round_trips(tmp_path):
    msg = 'could not apply C:\\tmp\\gc-job.yaml: "forbidden"\nsecond line\ttabbed'
    state = _finish(tmp_path, "down", msg)
    assert state["msg"] == msg
    assert state["status"] == "down"
