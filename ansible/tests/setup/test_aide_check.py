"""Guards on the weekly AIDE check's Kuma verdict (#4594).

The cron used to pipe `aide --check` into `logger`, so the pipeline's status was logger's and an
AIDE that checked nothing for weeks read the same as one that passed (#4591). aide-check.sh keeps
AIDE's exit code, pushes it to one Kuma tile per host, and holds five properties:

AN ERROR EXIT PUSHES DOWN. Anything outside 0-7 means the check did not complete.

A DIFFERENCE NO PACKAGE EXPLAINS PUSHES DOWN (#4599). Package upgrades change files every week,
so a difference a package's digest accounts for, or one outside the paged trees, pushes UP with
its counts; the script's `DECIDED:` marker has the measurement.

THE BASELINE ADVANCES ONLY ON UP. A DOWN keeps aide.db until `--accept`, and a missing aide.db
is never created.

ONE TOKEN PER HOST. A shared token would let one host's `up` cover another whose check stopped.

THE DEADLINE COVERS A WEEK. The producer is a weekly cron, so a shorter deadline fires DOWN
with nothing wrong, and one past two weeks lets a second missed run go unreported.

Run: uv run pytest ansible/tests/setup/test_aide_check.py
"""

import re

from _aide_check_run import (
    CHANGED_CONTENT,
    CLEAN_REPORT,
    DIFF_REPORT,
    ERROR_REPORT,
    _run_check,
)
from _helpers import ROLES, load_tasks, walk_tasks
from _kuma_monitors import entity, tile_is_gated_on
from _setup_render import render_setup_text

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
    assert (tmp_path / "aide-db" / "aide.db").read_text() == "last week\n"
    # The durable record of the verdict, which Loki keeps.
    assert any(line.startswith("aide-check: status=down ") for line in logged), logged


def test_a_held_lock_pushes_down_without_starting_a_second_scan(tmp_path):
    """A scan still running from last week holds the lock, so this week's run reports it (#5785)."""
    code, pushed, logged, ran = _run_check(tmp_path, CLEAN_REPORT, rc=0, lock_held=True)
    assert not ran, "a second aide scan started beside the one holding the lock"
    assert pushed[0] == "down", pushed
    assert "previous aide-check run still holds" in pushed[1]
    assert any(line.startswith("aide-check: status=down") for line in logged), logged
    assert code == 1


def _textfile(root, check):
    """The `homelab_check_*` values the run wrote, keyed by series name."""
    prom = (root / "textfile" / f"{check}.prom").read_text()
    return dict(
        re.findall(rf'^(homelab_check_\w+)\{{check="{check}"\}} (\d+)$', prom, re.M)
    )


def test_a_completed_check_writes_success_1_with_the_tiles_deadline(tmp_path):
    """The liveness path that outlives Kuma (#6168): the file exists even when no push does."""
    _run_check(tmp_path, DIFF_REPORT, rc=1, token="")
    series = _textfile(tmp_path, "aide-check")
    assert series["homelab_check_success"] == "1"
    assert series["homelab_check_deadline_seconds"] == "626400"


def test_an_error_exit_and_a_held_lock_each_write_success_0(tmp_path):
    for name, held in (("error", False), ("held", True)):
        root = tmp_path / name
        root.mkdir()
        _run_check(root, ERROR_REPORT, rc=17, lock_held=held)
        assert _textfile(root, "aide-check")["homelab_check_success"] == "0", name


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


def test_a_report_longer_than_a_pipe_buffer_logs_no_broken_pipe(tmp_path):
    """Every host logged `printf: write error: Broken pipe` on 2026-11-02 at the report cap."""
    long_report = DIFF_REPORT + f"{CHANGED_CONTENT}: /home/ubuntu/x\n" * 20000
    _run_check(tmp_path, long_report, rc=4, cap=3, ignore_sigpipe=True)
    assert "Broken pipe" not in (tmp_path / "stderr").read_text()


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
