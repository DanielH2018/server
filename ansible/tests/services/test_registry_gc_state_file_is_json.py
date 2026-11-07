"""registry-gc's state file is JSON whatever its message holds.

`finish()` writes `/var/lib/registry-gc/state.json` from `MSG`, which carries crane and kubectl
error text. An operator reads the file with `jq` after a run, so a message that breaks the JSON
breaks that read on the run whose message mattered (#4606).

`finish()` is sliced out of the RENDERED script and run with `curl`, `logger` and `kuma_push`
stubbed as shell functions. The `curl` stub is load-bearing: `finish()` reads the unit's
`healthchecks_ping_key` credential whenever it is readable, so a run with the credential and no
stub would send a real `/fail` ping to the live `registry-gc` dead-man.
"""

import json
import os
import re
import shlex
from pathlib import Path

from _shell_render import rendered_shell_function
from lib.proc_testing import run
from lib.repo_paths import REPO

FINISH = rendered_shell_function(
    "components", "registry-gc", "registry-gc.sh.j2", "finish"
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


def _pings(tmp_path: Path, status: str, key: str | None) -> list[str]:
    """Run finish() under the credential the unit imports, and return curl's URLs."""
    unit = REPO / "hosts/components/registry-gc/units/homelab-registry-gc.service"
    (credential,) = re.findall(r"^ImportCredential=(\S+)$", unit.read_text(), re.M)
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    if key is not None:
        (credentials / credential).write_text(key)
    calls = tmp_path / "curl-calls"
    script = f"""
STATE_DIR={shlex.quote(str(tmp_path))}
STATE_FILE={shlex.quote(str(tmp_path / "state.json"))}
STATUS={shlex.quote(status)}
MSG=done
curl() {{ printf '%s\\n' "${{@: -1}}" >> {shlex.quote(str(calls))}; }}
logger() {{ :; }}
write_check_textfile() {{ :; }}
{FINISH}
finish
"""
    env = {**os.environ, "CREDENTIALS_DIRECTORY": str(credentials)}
    run(["bash", "-c", script], env=env, check=True)
    return calls.read_text().splitlines() if calls.exists() else []


def test_an_up_run_pings_the_check_with_the_key_the_unit_imports(tmp_path):
    assert _pings(tmp_path, "up", "fixture-key") == [
        "https://hc-ping.com/fixture-key/registry-gc"
    ]


def test_a_down_run_pings_the_fail_endpoint(tmp_path):
    assert _pings(tmp_path, "down", "fixture-key") == [
        "https://hc-ping.com/fixture-key/registry-gc/fail"
    ]


def test_a_missing_key_sends_no_ping(tmp_path):
    assert _pings(tmp_path, "up", None) == []
