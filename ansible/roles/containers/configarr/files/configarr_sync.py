#!/usr/bin/env python3
"""configarr sync host cron — runs the one-shot guide sync and reports health like the other host crons.

configarr reconciles Sonarr/Radarr quality profiles + custom formats from the TRaSH guides (plus the
local Anime release-group defense). It's a batch job: this wrapper runs `docker compose run --rm`
configarr, captures its exit code + output, and writes a {ts,ok,msg} state file that monitor-bridge
reads over a :ro bind mount and turns into the "Configarr Sync" Kuma monitor.

Replaces the raw `docker compose run` cron line so a failed sync actually pages — recyclarr's old
healthcheck watched only the scheduler process, so the 2026-06-10 v8 sync breakage was invisible.
Runs under the host's /usr/bin/python3 (3.12 floor — keep 3.12-clean, see
ansible/tests/test_host_scripts_py312.py) as the sys_user (in the docker group). Config is
non-secret and comes from the environment (the cron/deploy task sets CONFIGARR_COMPOSE).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configarr_status as cs  # noqa: E402  (sibling module, resolved via the sys.path insert)
from host_lib import atomic_write  # noqa: E402

COMPOSE_FILE = os.environ.get(
    "CONFIGARR_COMPOSE", "/home/ubuntu/server/containers/configarr/docker-compose.yml"
)
STATE_FILE = os.environ.get("CONFIGARR_STATE_FILE", "/var/lib/configarr/state.json")
SERVICE = os.environ.get("CONFIGARR_SERVICE", "configarr")
TIMEOUT = int(os.environ.get("CONFIGARR_TIMEOUT_S", "300"))


def log(*args) -> None:
    print("[%s]" % time.strftime("%Y-%m-%dT%H:%M:%S"), *args, flush=True)


def run_sync():
    """Run the one-shot configarr container. Returns (returncode, combined stdout+stderr)."""
    try:
        proc = subprocess.run(
            ["docker", "compose", "-f", COMPOSE_FILE, "run", "--rm", "-T", SERVICE],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return 124, "configarr sync timed out after %ss" % TIMEOUT
    except OSError as e:
        return 125, "could not launch configarr: %s" % e
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    log("configarr sync starting")
    rc, output = run_sync()
    ok, msg = cs.evaluate(rc, output)
    log("OK  " if ok else "DOWN", msg)
    atomic_write(
        STATE_FILE, json.dumps({"ts": int(time.time()), "ok": bool(ok), "msg": msg})
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
