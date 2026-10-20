"""Guards on the weekly AIDE check's Kuma verdict (#4594).

The cron used to pipe `aide --check` into `logger`, so the pipeline's status was logger's and an
AIDE that checked nothing for weeks read the same as one that passed (#4591). aide-check.sh keeps
AIDE's exit code, pushes it to one Kuma tile per host, and holds four properties:

AN ERROR EXIT PUSHES DOWN. Anything outside 0-7 means the check did not complete.

DIFFERENCES PUSH UP, WITH THEIR COUNTS. Package upgrades change files every week, so a DOWN on
differences would be red every Monday; the script's `DECIDED:` marker has the evidence.

ONE TOKEN PER HOST. A shared token would let one host's `up` cover another whose check stopped.

THE DEADLINE COVERS A WEEK. The producer is a weekly cron, so a shorter deadline fires DOWN
with nothing wrong, and one past two weeks lets a second missed run go unreported.

Run: uv run pytest ansible/tests/setup/test_aide_check.py
"""

import fcntl
import os
import re

from lib.proc_testing import fake_bin, run, write_exec
from _helpers import ROLES, load_tasks, walk_tasks
from _kuma_monitors import entity, tile_is_gated_on
from _setup_render import render_setup_text
from _shell_render import render_shell_script

SCRIPT = ("setup", "initial_setup", "aide-check.sh.j2")
PUSH_ENV = "aide-kuma-push.env.j2"
INTEGRITY = ROLES / "setup" / "initial_setup" / "tasks" / "integrity.yml"
CRON_NAME = "Weekly AIDE file integrity check"

# Each host's tile and the variable it is gated on.
TILES = {
    "daniel-box": ("aide-daniel-box.json", "aide_daniel_box_push_token"),
    "daniel-server": ("aide-daniel-server.json", "aide_daniel_server_push_token"),
    "daniel-pi": ("aide-daniel-pi.json", "aide_daniel_pi_push_token"),
}
WEEK_S = 7 * 24 * 3600

REAL_LIB = "/usr/local/lib/kuma-push-lib.sh"
REAL_ENV = "/etc/homelab/aide-kuma-push.env"
REAL_AIDE = "/usr/bin/nice -n19 /usr/bin/ionice -c3 /usr/bin/aide"
REAL_CAP = "REPORT_MAX_LINES=2000"
REAL_LOCK = "LOCK_FILE=/run/lock/aide-check.lock"

CLEAN_REPORT = """\
Start timestamp: 2026-10-19 03:00:01 +0000 (AIDE 0.18.6)
AIDE found NO differences between database and filesystem. Looks okay!!
"""
DIFF_REPORT = """\
Start timestamp: 2026-10-19 03:00:01 +0000 (AIDE 0.18.6)
AIDE found differences between database and filesystem!!

Summary:
  Total number of entries:\t266585
  Added entries:\t\t2
  Removed entries:\t\t0
  Changed entries:\t\t3

---------------------------------------------------
Added entries:
---------------------------------------------------

f++++++++++++++++: /etc/new-one
f++++++++++++++++: /etc/new-two
"""
# The line every host logged each Monday until #4591, and the exit it came with.
ERROR_REPORT = (
    "  ERROR: missing configuration (use '--config' '--before' or '--after' "
    "command line parameter)\n"
)

# Records each call as `tag: message`: the message argument when one is given, else every line
# on stdin, which is how the script hands over the report.
LOGGER_STUB = """\
#!/bin/sh
tag=""
while [ $# -gt 0 ]; do
  case "$1" in
    -t) tag=$2; shift 2 ;;
    -p) shift 2 ;;
    *) break ;;
  esac
done
if [ $# -gt 0 ]; then
  printf '%s: %s\\n' "$tag" "$*" >> "$LOGGER_OUT"
else
  while IFS= read -r line; do printf '%s: %s\\n' "$tag" "$line" >> "$LOGGER_OUT"; done
fi
"""


def _run_check(
    tmp_path,
    report: str,
    rc: int,
    token: str = "stubtoken",
    cap: int = 2000,
    lock_held: bool = False,
):
    """Render aide-check.sh and run it with aide stubbed to print `report` and exit `rc`.

    Only absolute paths are repointed: the push lib (a recording stub), the token env file, the
    lock file and the nice/ionice/aide invocation. `lock_held` holds the lock from this process
    for the run, as a scan still running from the previous week would. Returns (exit code,
    pushed lines or None, logged lines, whether aide ran).
    """
    body = render_shell_script(
        *SCRIPT,
        overrides={"domain": "example.test", "k3s_metallb_ingress_vip": "10.0.0.240"},
    )
    for needle in (REAL_LIB, REAL_ENV, REAL_AIDE, REAL_CAP, REAL_LOCK):
        assert needle in body, (
            f"{needle} is no longer in aide-check.sh; this harness repoints it"
        )

    lib = tmp_path / "kuma-push-lib.sh"
    lib.write_text(
        'kuma_push() { printf \'%s\\n%s\\n\' "$1" "$2" > "$KUMA_PUSH_OUT"; }\n'
    )
    env_file = tmp_path / "push.env"
    env_file.write_text(f"AIDE_PUSH_TOKEN={token}\n")
    lock_file = tmp_path / "aide-check.lock"
    report_file = tmp_path / "report"
    report_file.write_text(report)
    aide = write_exec(
        tmp_path / "aide",
        f'touch "$AIDE_RAN"\ncat {report_file}\nexit {rc}\n',
    )
    body = (
        body.replace(REAL_LIB, str(lib))
        .replace(REAL_ENV, str(env_file))
        .replace(REAL_AIDE, str(aide))
        .replace(REAL_CAP, f"REPORT_MAX_LINES={cap}")
        .replace(REAL_LOCK, f"LOCK_FILE={lock_file}")
    )
    script = write_exec(tmp_path / "aide-check.sh", body)
    pushed, logged, ran = (tmp_path / n for n in ("pushed", "logged", "ran"))
    with open(lock_file, "w") as holder:
        if lock_held:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run(
            [str(script)],
            env={
                **os.environ,
                "KUMA_PUSH_OUT": str(pushed),
                "LOGGER_OUT": str(logged),
                "AIDE_RAN": str(ran),
            },
            stub_bin=fake_bin(tmp_path / "bin", logger=LOGGER_STUB),
        )
    return (
        result.returncode,
        pushed.read_text().splitlines() if pushed.exists() else None,
        logged.read_text().splitlines() if logged.exists() else [],
        ran.exists(),
    )


def test_a_clean_check_pushes_up(tmp_path):
    code, pushed, _, _ = _run_check(tmp_path, CLEAN_REPORT, rc=0)
    assert pushed[0] == "up", pushed
    assert "no differences" in pushed[1]
    assert code == 0


def test_differences_push_up_with_their_counts(tmp_path):
    code, pushed, _, _ = _run_check(tmp_path, DIFF_REPORT, rc=1)
    assert pushed[0] == "up", pushed
    assert "2 added, 0 removed, 3 changed" in pushed[1], pushed[1]
    assert code == 0


def test_an_error_exit_pushes_down_with_aides_error_line(tmp_path):
    """The #4591 failure: the check exits on its first line, so nothing was checked."""
    code, pushed, logged, _ = _run_check(tmp_path, ERROR_REPORT, rc=17)
    assert pushed[0] == "down", pushed
    assert "aide exited 17" in pushed[1]
    assert "ERROR: missing configuration" in pushed[1], pushed[1]
    assert code == 1
    # The durable record `probe.py alerts` reconstructs the episode from.
    assert any(line.startswith("aide-check: status=down ") for line in logged), logged


def test_a_held_lock_pushes_down_without_starting_a_second_scan(tmp_path):
    """A scan still running from last week holds the lock, so this week's run reports it (#5785)."""
    code, pushed, logged, ran = _run_check(tmp_path, CLEAN_REPORT, rc=0, lock_held=True)
    assert not ran, "a second aide scan started beside the one holding the lock"
    assert pushed[0] == "down", pushed
    assert "previous aide-check run still holds" in pushed[1]
    assert any(line.startswith("aide-check: status=down") for line in logged), logged
    assert code == 1


def test_a_free_lock_lets_the_scan_run(tmp_path):
    code, pushed, _, ran = _run_check(tmp_path, CLEAN_REPORT, rc=0, lock_held=False)
    assert ran and pushed[0] == "up" and code == 0, pushed


# Paths AIDE must skip, and paths beside them it must still check (#5785). The second list is
# what keeps an exclusion from widening: the primary checkout, the repo's hooks and the
# executables under ~/.local all sit next to an excluded directory.
EXCLUDED = (
    "/home/ubuntu/server/.claude/worktrees/fix-5785/.venv/bin/python",
    "/home/ubuntu/.cache/uv/archive-v0/x/lib.py",
    "/home/ubuntu/.npm/_npx/a1/node_modules/x/index.js",
    "/home/ubuntu/.local/state/fanout-review/5685-stop-hook/out.json",
    "/run/k3s/containerd/io.containerd.runtime.v2.task/k8s.io/d98e30b5/rootfs/app/index.js",
    "/run/containerd/io.containerd.runtime.v2.task/moby/4f1c/config.json",
    "/run/netns/cni-289be4cb-2e30-a45f-2d8d-70d64532ae4f",
    "/var/lib/kubelet/plugins/kubernetes.io/csi/driver.longhorn.io/x/globalmount/db",
    "/var/lib/kubelet/allocated_pods_state",
    "/home/ubuntu/.kube/cache/discovery/127.0.0.1_6443/v1/serverresources.json",
    "/srv/media/media/movies/Film (2026)/Film.mkv",
    "/swap.img",
    "/swapfile",
)
CHECKED = (
    "/home/ubuntu/server/ansible/deploy.yml",
    "/home/ubuntu/server/.claude/hooks/block-footguns.py",
    "/home/ubuntu/.local/share/uv/python/cpython-3.14/bin/python3",
    "/home/ubuntu/.local/bin/uv",
    "/etc/passwd",
    "/var/lib/rancher/k3s/data/current/bin/k3s",
    "/var/lib/kubelet/config.yaml",
    "/home/ubuntu/.kube/config",
    "/home/ubuntu/.claude/settings.json",
    "/srv/other/file",
    "/swap.img.bak",
)


def _excluded(path: str, conf: str) -> bool:
    """Whether a `!` rule in `conf` matches `path`, matched the way AIDE does: a regex anchored at
    the path's start."""
    rules = [line[1:] for line in conf.splitlines() if line.startswith("!")]
    return any(re.match(rule, path) for rule in rules)


def test_worktrees_and_caches_are_excluded_and_their_neighbours_are_not():
    conf = render_setup_text(
        "initial_setup", "98_aide_local.conf.j2", {"sys_user": "ubuntu"}
    )
    assert [p for p in EXCLUDED if not _excluded(p, conf)] == []
    assert [p for p in CHECKED if _excluded(p, conf)] == []


# Debian's /etc/aide/aide.conf: `@@x_include /etc/aide/aide.conf.d ^[a-zA-Z0-9_-]+$`. A
# conf.d file whose name fails this is skipped silently, and `aide --config-check` still passes.
DEBIAN_INCLUDE_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


def _aide_local_dest() -> str:
    dests = [
        task["ansible.builtin.template"]["dest"]
        for task in walk_tasks(load_tasks(INTEGRITY))
        if (task.get("ansible.builtin.template") or {}).get("src")
        == "98_aide_local.conf.j2"
    ]
    assert len(dests) == 1, (
        f"expected one task deploying 98_aide_local.conf.j2, got {dests}"
    )
    return dests[0]


def test_the_include_pattern_skips_a_dotted_name_and_loads_a_plain_one():
    assert not DEBIAN_INCLUDE_NAME.match("98_aide_local.conf")
    assert DEBIAN_INCLUDE_NAME.match("98_aide_local")


def test_the_exclusions_deploy_under_a_name_aide_loads():
    """From 2026-05-31 to 2026-10-20 the file was `98_aide_local.conf` and no rule in it ran."""
    dest = _aide_local_dest()
    assert dest.startswith("/etc/aide/aide.conf.d/"), dest
    assert DEBIAN_INCLUDE_NAME.match(dest.rsplit("/", 1)[1]), (
        f"{dest} fails aide.conf's include pattern, so AIDE never reads it"
    )


def test_the_check_still_runs_when_no_token_is_set(tmp_path):
    """A host whose secret is missing keeps its weekly check; only the push is skipped."""
    _, pushed, logged, ran = _run_check(tmp_path, CLEAN_REPORT, rc=0, token="")
    assert ran, "aide did not run"
    assert pushed is None, "pushed with no token"
    assert any("AIDE_PUSH_TOKEN not set" in line for line in logged), logged


def test_a_long_report_is_capped_with_a_line_saying_so(tmp_path):
    _, _, logged, _ = _run_check(tmp_path, DIFF_REPORT, rc=1, cap=3)
    report_lines = [line for line in logged if line.startswith("aide: ")]
    assert report_lines[:3] == [
        "aide: Start timestamp: 2026-10-19 03:00:01 +0000 (AIDE 0.18.6)",
        "aide: AIDE found differences between database and filesystem!!",
        "aide: ",
    ], report_lines
    assert report_lines[3].startswith("aide: report truncated: logged 3 of "), (
        report_lines
    )
    assert len(report_lines) == 4, report_lines


def test_each_host_pushes_its_own_token():
    """Rendered once per host with a different sentinel behind each variable (#3202)."""
    sentinels = {var: f"{host}-sentinel" for host, (_, var) in TILES.items()}
    for host in TILES:
        rendered = render_setup_text(
            "initial_setup", PUSH_ENV, {"inventory_hostname": host, **sentinels}
        )
        carried = [s for s in sentinels.values() if s in rendered]
        assert carried == [f"{host}-sentinel"], (
            f"{host} must carry its own token and no other; it carries {carried}"
        )


def test_each_host_has_a_tile_gated_on_its_own_token():
    """An ungated tile sits red from creation until its secret exists."""
    for host, (tile, gate_var) in TILES.items():
        assert entity(tile, gate_var)["name"] == f"AIDE Integrity ({host})"
        assert tile_is_gated_on(tile, gate_var), (
            f"{tile} renders with {gate_var} empty, so it sits red from creation"
        )


def test_the_push_deadline_covers_one_week_and_not_two():
    cron = next(
        task["ansible.builtin.cron"]
        for task in walk_tasks(load_tasks(INTEGRITY))
        if (task.get("ansible.builtin.cron") or {}).get("name") == CRON_NAME
    )
    assert cron["weekday"] == "1" and cron["hour"] == "3", (
        "this test reads the cron as weekly; give it the new period if that changes"
    )
    for tile, gate_var in TILES.values():
        interval = entity(tile, gate_var)["interval"]
        assert WEEK_S < interval < 2 * WEEK_S, (
            f"{tile}'s deadline {interval}s must exceed the cron's weekly period, and stay "
            "under two so a second missed run cannot pass unreported"
        )
