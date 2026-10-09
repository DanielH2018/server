"""Guards on the Loki read-route witness — the host cron that watches a route no pod can reach.

loki-homelab's read route has one caller, `probe.py` running as a host process, and its guard is a
ClientIP set of node-owned addresses. Three properties keep the witness honest, and each fails in a
way that reads as success:

TWO HOSTS, TWO TOKENS. Which address a host arrives as depends on where the traefik pod sits,
so the route can be dead from one host and healthy from the other. A shared push token
would let either host's `up` satisfy Kuma's deadline and mask the other — the same trap the
UPS secondary watchdog avoids.

THE DEADLINE EXCEEDS THE PRODUCER'S PERIOD. A push monitor whose interval is shorter than its cron's
period fires DOWN with nothing wrong.

THERE IS A WAY OUT. A host dropped from `loki_route_witness_hosts` must lose the cron, or it keeps
pushing a monitor the manifest no longer declares — a 404 that kuma-push-lib retries three times an
hour, forever.

Run: uv run pytest ansible/tests/setup/test_loki_route_witness.py
"""

import os

from lib.proc_testing import fake_bin, path_with, run, write_exec
from _helpers import ALL_VARS, ANSIBLE
from _kuma_monitors import entity, tile_is_gated_on
from _setup_render import render_setup_text
from _shell_render import render_shell_script, rendered_shell_text
from lib import yaml_fast

ALL_VARS_VALUES = yaml_fast.safe_load(ALL_VARS.read_text())
CRONS = (
    ANSIBLE / "roles" / "setup" / "initial_setup" / "tasks" / "crons.yml"
).read_text()
CRON_TASKS = yaml_fast.safe_load(CRONS)
WITNESS = ("setup", "initial_setup", "loki-read-route-health.sh.j2")
PUSH_ENV = "loki-route-kuma-push.env.j2"

# The two tiles and the variable each is gated on, one per witnessing host.
TILES = {
    "daniel-box": ("loki-read-route-daniel-box.json", "loki_route_witness_push_token"),
    "daniel-server": (
        "loki-read-route-daniel-server.json",
        "loki_route_witness_daniel_server_push_token",
    ),
}
# Two values no secret holds, so a render carrying one carries THAT host's value.
SENTINELS = {
    "loki_route_witness_push_token": "loki-witness-box-sentinel",
    "loki_route_witness_daniel_server_push_token": "loki-witness-server-sentinel",
}

CRON_NAME = "Loki read-route witness"
# The monitor interval declared for both tiles, and the timer period it has to cover.
MONITOR_INTERVAL_S = 7200
HOURLY_S = 3600
REAL_LIB = "/usr/local/lib/kuma-push-lib.sh"
REAL_ENV = "/etc/homelab/loki-route-kuma-push.env"


def _timer_import() -> dict:
    """The import of kuma_check_timer.yml that schedules the witness; its vars are the schedule."""
    for task in _flatten(CRON_TASKS):
        target = task.get("ansible.builtin.import_tasks") or ""
        if (
            target.endswith("common/tasks/kuma_check_timer.yml")
            and (task.get("vars") or {}).get("kuma_check_cron_name") == CRON_NAME
        ):
            return task
    raise AssertionError(
        f"no kuma_check_timer.yml import replaces the `{CRON_NAME}` cron"
    )


def _flatten(tasks):
    for task in tasks or []:
        yield task
        yield from _flatten(task.get("block"))


def test_both_cluster_nodes_witness_and_nothing_else_does():
    """Non-vacuity, and the reason there are two: one host cannot see a route that is dead only from the other."""
    assert ALL_VARS_VALUES["loki_route_witness_hosts"] == [
        "daniel-box",
        "daniel-server",
    ], (
        "both prod cluster nodes witness the route from different source addresses; "
        "daniel-pi is not in its ClientIP set and daniel-stage is a different cluster"
    )


def test_each_host_pushes_its_own_token():
    """A shared token lets one host's `up` cover the other's dead route.

    Asserted by rendering the env file once per host with a DIFFERENT sentinel behind each
    variable: the per-host SELECTION is the claim, and a render at inventory values cannot show
    it because both resolve to the same empty string (#3202).
    """
    rendered = {
        host: render_setup_text(
            "initial_setup", PUSH_ENV, {"inventory_hostname": host, **SENTINELS}
        )
        for host in TILES
    }
    box, server = (SENTINELS[v] for _, v in TILES.values())
    assert box in rendered["daniel-box"] and server not in rendered["daniel-box"], (
        "daniel-box must carry its own value; it renders "
        f"{rendered['daniel-box'].splitlines()[-1]!r}"
    )
    assert (
        server in rendered["daniel-server"] and box not in rendered["daniel-server"]
    ), (
        "the env template must select per host, or both nodes push the same monitor; "
        f"daniel-server renders {rendered['daniel-server'].splitlines()[-1]!r}"
    )


def test_both_tiles_are_declared_and_gated_on_their_own_token():
    """An ungated tile sits red from creation until its secret exists.

    Read off the render: `entity` fails when the tile is absent with its gate armed, and
    `tile_is_gated_on` fails when the tile is there with the gate open. Together they pin each
    tile to THAT variable, which a substring match on the `{% if %}` line cannot do.
    """
    for host, (tile, gate_var) in TILES.items():
        assert entity(tile, gate_var)["name"] == f"Loki Read Route ({host})", (
            f"{tile} must be the tile named for {host}"
        )
        assert tile_is_gated_on(tile, gate_var), (
            f"{tile} renders with {gate_var} empty, so it sits red from creation"
        )


def test_the_push_deadline_exceeds_the_timer_period():
    """A deadline shorter than the producer's period fires DOWN with nothing wrong."""
    on_calendar = _timer_import()["vars"]["kuma_check_on_calendar"]
    assert on_calendar.startswith("*-*-* *:"), (
        "this test reads the timer as hourly; give it a period if that changes"
    )
    assert MONITOR_INTERVAL_S >= 2 * HOURLY_S
    for tile, gate_var in TILES.values():
        assert entity(tile, gate_var)["interval"] == MONITOR_INTERVAL_S, (
            f"{tile}'s rendered interval is not {MONITOR_INTERVAL_S}s, so the deadline this "
            "test compares against the timer period is not the one Kuma gets"
        )


def test_a_host_dropped_from_the_list_loses_the_timer_and_the_cron():
    """The way out. Without it a retired witness pushes a monitor that no longer exists."""
    variables = _timer_import()["vars"]
    assert "loki_route_witness_hosts" in variables["kuma_check_state"], (
        "kuma_check_state must follow the witness list, or a dropped host keeps the timer"
    )
    assert variables["kuma_check_cron_name"] == CRON_NAME, (
        "the import must name the cron it replaces, or a host runs both"
    )


def _run_witness(tmp_path, route_rc: int) -> tuple[int, str]:
    """Render the witness and run it with the reader stubbed to exit `route_rc`.

    Only absolute paths are repointed: the push lib (a recording stub), the token env file,
    and the two `uv` binaries, whose stub answers the reader invocation with `route_rc` and
    prints a body for anything else. Returns (exit code, pushed status).
    """
    body = render_shell_script(
        *WITNESS,
        overrides={
            "domain": "example.test",
            "k3s_metallb_ingress_vip": "10.0.0.240",
            "sys_user": "ubuntu",
            "host_python_version": "3.12",
            "loki_route_witness_boot_grace_s": 0,
        },
    )
    for needle in (
        REAL_LIB,
        REAL_ENV,
        "/home/ubuntu/.local/bin/uv",
        "/usr/local/bin/uv",
    ):
        assert needle in body, (
            f"{needle} is no longer in the witness; this harness repoints it"
        )

    pushed = tmp_path / "pushed"
    lib = tmp_path / "kuma-push-lib.sh"
    lib.write_text(
        'kuma_push() { printf \'%s\\n\' "$1" > "$KUMA_PUSH_OUT"; return 0; }\n'
        "boot_grace_active() { return 1; }\n"
    )
    env_file = tmp_path / "push.env"
    env_file.write_text("LOKI_ROUTE_WITNESS_PUSH_TOKEN=stubtoken\n")
    uv = write_exec(
        tmp_path / "uv",
        "#!/usr/bin/env bash\n"
        'case "$*" in *loki_route_health.py*) echo verdict; exit "$STUB_ROUTE_RC" ;;'
        " *) echo body ;; esac\n",
    )
    binstub = fake_bin(tmp_path / "bin", logger="#!/bin/sh\nexit 0\n")

    body = (
        body.replace(REAL_LIB, str(lib))
        .replace(REAL_ENV, str(env_file))
        .replace("/home/ubuntu/.local/bin/uv", str(uv))
        .replace("/usr/local/bin/uv", str(uv))
    )
    script = write_exec(tmp_path / "loki-read-route-health.sh", body)
    result = run(
        [str(script)],
        env={
            **os.environ,
            "PATH": path_with(binstub),
            "KUMA_PUSH_OUT": str(pushed),
            "STUB_ROUTE_RC": str(route_rc),
        },
        check=False,
    )
    return result.returncode, pushed.read_text().strip()


def test_a_down_verdict_exits_nonzero_so_the_timer_reruns_it(tmp_path):
    """The kuma-check timer's contract: exit 1 after a down push, and Restart=on-failure reruns."""
    code, status = _run_witness(tmp_path, route_rc=1)
    assert status == "down"
    assert code == 1


def test_an_up_verdict_exits_zero_so_the_timer_rests(tmp_path):
    code, status = _run_witness(tmp_path, route_rc=0)
    assert status == "up"
    assert code == 0


def test_the_verdict_reads_the_body_rather_than_the_exit_code():
    """probe.py's curl has no `-f`, so `404 page not found` arrives with exit 0."""
    assert "loki_route_health.py" in rendered_shell_text(*WITNESS), (
        "the witness must decide on the response body — an exit-code verdict reads UP through "
        "exactly the outage this cron exists to catch"
    )


def test_the_boot_grace_is_shorter_than_the_cron_period():
    """kuma-push-lib's contract: at most one slot may ever be skipped."""
    assert ALL_VARS_VALUES["loki_route_witness_boot_grace_s"] < HOURLY_S
