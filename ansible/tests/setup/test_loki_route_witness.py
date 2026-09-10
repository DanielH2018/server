"""Guards on the Loki read-route witness — the host cron that watches a route no pod can reach.

loki-homelab's read route has one caller, `probe.py` running as a host process, and its guard is a
ClientIP set of node-owned addresses. Three properties keep the witness honest, and each fails in a
way that reads as success:

TWO HOSTS, TWO TOKENS. Which address a host arrives as depends on where the traefik pod sits, which
is why #1693 left the route dead from daniel-server and healthy from daniel-box. A shared push token
would let either host's `up` satisfy Kuma's deadline and mask the other — the trap issue #952
records for the UPS secondary watchdog.

THE DEADLINE EXCEEDS THE PRODUCER'S PERIOD. A push monitor whose interval is shorter than its cron's
period fires DOWN with nothing wrong, which is how the Cloudflare DDNS tiles were read as broken.

THERE IS A WAY OUT. A host dropped from `loki_route_witness_hosts` must lose the cron, or it keeps
pushing a monitor the manifest no longer declares — a 404 that kuma-push-lib retries three times an
hour, forever.

Run: uv run pytest ansible/tests/setup/test_loki_route_witness.py
"""

from _helpers import ANSIBLE
from lib import yaml_fast

GROUP_VARS = yaml_fast.safe_load(
    (ANSIBLE / "inventory" / "group_vars" / "all.yml").read_text()
)
CRONS = (
    ANSIBLE / "roles" / "setup" / "initial_setup" / "tasks" / "crons.yml"
).read_text()
CRON_TASKS = yaml_fast.safe_load(CRONS)
SCRIPT = (
    ANSIBLE
    / "roles"
    / "setup"
    / "initial_setup"
    / "templates"
    / "loki-read-route-health.sh.j2"
).read_text()
PUSH_ENV = (
    ANSIBLE
    / "roles"
    / "setup"
    / "initial_setup"
    / "templates"
    / "loki-route-kuma-push.env.j2"
).read_text()
MONITORS = (
    ANSIBLE / "roles" / "k8s" / "uptime-kuma" / "templates" / "static-monitors.yaml.j2"
).read_text()

CRON_NAME = "Loki read-route witness"
# The monitor interval declared for both tiles, and the cron period it has to cover.
MONITOR_INTERVAL_S = 7200
HOURLY_S = 3600


def _cron_task(state: str) -> dict:
    """The scheduling task, or the removal one. `state` is `present` (implicit) or `absent`."""
    for task in _flatten(CRON_TASKS):
        cron = task.get("ansible.builtin.cron") or task.get("cron") or {}
        if cron.get("name") != CRON_NAME:
            continue
        if cron.get("state", "present") == state:
            return task
    raise AssertionError(f"no `{CRON_NAME}` cron task with state={state}")


def _flatten(tasks):
    for task in tasks or []:
        yield task
        yield from _flatten(task.get("block"))


def test_both_cluster_nodes_witness_and_nothing_else_does():
    """Non-vacuity, and the reason there are two: one host cannot see #1693."""
    assert GROUP_VARS["loki_route_witness_hosts"] == ["daniel-box", "daniel-server"], (
        "both prod cluster nodes witness the route from different source addresses; "
        "daniel-pi is not in its ClientIP set and daniel-stage is a different cluster"
    )


def test_each_host_pushes_its_own_token():
    """A shared token lets one host's `up` cover the other's dead route (issue #952)."""
    assert "loki_route_witness_daniel_server_push_token" in PUSH_ENV
    assert "loki_route_witness_push_token" in PUSH_ENV
    assert "inventory_hostname == 'daniel-server'" in PUSH_ENV, (
        "the env template must select the token per host, or both nodes push the same monitor"
    )


def test_both_tiles_are_declared_and_gated_on_their_own_token():
    """An ungated tile sits red from creation until its secret exists."""
    assert '"Loki Read Route (daniel-box)"' in MONITORS
    assert '"Loki Read Route (daniel-server)"' in MONITORS
    assert "{% if loki_route_witness_push_token | default('') %}" in MONITORS
    assert (
        "{% if loki_route_witness_daniel_server_push_token | default('') %}" in MONITORS
    )


def test_the_push_deadline_exceeds_the_cron_period():
    """A deadline shorter than the producer's period fires DOWN with nothing wrong."""
    cron = _cron_task("present")["ansible.builtin.cron"]
    assert "hour" not in cron, (
        "this test reads the cron as hourly; give it a period if that changes"
    )
    assert MONITOR_INTERVAL_S >= 2 * HOURLY_S
    assert f'"interval": {MONITOR_INTERVAL_S}' in MONITORS


def test_a_host_dropped_from_the_list_loses_the_cron():
    """The way out. Without it a retired witness pushes a monitor that no longer exists."""
    task = _cron_task("absent")
    assert task["ansible.builtin.cron"]["state"] == "absent"


def test_the_verdict_reads_the_body_rather_than_the_exit_code():
    """probe.py's curl has no `-f`, so `404 page not found` arrives with exit 0."""
    assert "loki_route_health.py" in SCRIPT, (
        "the witness must decide on the response body — an exit-code verdict reads UP through "
        "exactly the outage this cron exists to catch"
    )


def test_the_boot_grace_is_shorter_than_the_cron_period():
    """kuma-push-lib's contract: at most one slot may ever be skipped."""
    assert GROUP_VARS["loki_route_witness_boot_grace_s"] < HOURLY_S
