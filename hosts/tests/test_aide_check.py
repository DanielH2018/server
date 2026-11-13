"""Guards on the weekly AIDE check's textfile verdict (#4594).

The cron used to pipe `aide --check` into `logger`, so the pipeline's status was logger's and an
AIDE that checked nothing for weeks read the same as one that passed (#4591). aide-check.sh keeps
AIDE's exit code, logs it and writes it as a textfile verdict, and holds five properties. It
pushed it to one Kuma tile per host until M6 batch B9's Cutover-2, so a test says "pushes DOWN"
for a run whose logged status is `down`:

AN ERROR EXIT PUSHES DOWN. Anything outside 0-7 means the check did not complete.

A DIFFERENCE NO PACKAGE EXPLAINS PUSHES DOWN (#4599). Package upgrades change files every week,
so a difference a package's digest accounts for, or one outside the paged trees, pushes UP with
its counts; the script's `DECIDED:` marker has the measurement.

THE BASELINE ADVANCES ONLY ON UP. A DOWN keeps aide.db until `--accept`, and a missing aide.db
is never created.

ONE TOKEN PER HOST. A shared token would let one host's `up` cover another whose check stopped.

THE DEADLINE COVERS A WEEK. The producer is a weekly cron, so a shorter deadline fires DOWN
with nothing wrong, and one past two weeks lets a second missed run go unreported.

Run: uv run pytest hosts/tests/test_aide_check.py
"""

import re

from _aide_check_run import (
    CHANGED_CONTENT,
    CLEAN_REPORT,
    DIFF_REPORT,
    ERROR_REPORT,
    SCRIPT,
    _run_check,
)
from _host_packages import HOSTS, package, shipped_text
from _setup_render import render_component_text
from _shell_render import render_shell_script

TIMER = "/usr/lib/systemd/system/homelab-aide-check.timer"
# The hosts whose package lists aide-scan; daniel-pi's does not.
AIDE_HOSTS = [host for host in HOSTS if TIMER in package(host).files]


def test_only_the_hosts_listing_aide_scan_ship_aide():
    """daniel-pi's scan outgrew its 456 MB and reset it twice on 2026-11-12 (#8084)."""
    assert AIDE_HOSTS == ["daniel-box", "daniel-server"]
    assert [path for path in package("daniel-pi").files if "aide" in path] == []


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


def test_a_completed_check_writes_success_1_with_its_deadline(tmp_path):
    """The textfile is the only verdict channel since the push left (#6168)."""
    _run_check(tmp_path, DIFF_REPORT, rc=1)
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


AIDE_LOCAL_VARS = {"sys_user": "ubuntu"}


def _excluded(path: str, conf: str) -> bool:
    """Whether a `!` rule in `conf` matches `path`, matched the way AIDE does: a regex anchored at
    the path's start."""
    rules = [line[1:] for line in conf.splitlines() if line.startswith("!")]
    return any(re.match(rule, path) for rule in rules)


def test_worktrees_and_caches_are_excluded_and_their_neighbours_are_not():
    conf = render_component_text("aide-scan", "98_aide_local.conf.j2", AIDE_LOCAL_VARS)
    assert [p for p in EXCLUDED if not _excluded(p, conf)] == []
    assert [p for p in CHECKED if _excluded(p, conf)] == []


def _inode_skipped(path: str, conf: str) -> bool:
    """Whether a selection line in `conf` that matches `path` gives it a rule ending in `-i`."""
    for line in conf.splitlines():
        if not line.startswith("/"):
            continue
        regex, *_, rule = line.split()
        if re.match(regex, path) and rule.endswith("-i"):
            return True
    return False


# Live paths whose only change was a vfat inode number: daniel-box's ESP on 2026-11-09 and
# daniel-pi's firmware partition on 2026-11-10.
VFAT_PATHS = (
    "/boot/efi/EFI/ubuntu",
    "/boot/efi/EFI/ubuntu/grubx64.efi",
    "/boot/firmware",
    "/boot/firmware/README",
    "/boot/firmware/overlays/pwm.dtbo",
    "/boot/firmware/System Volume Information/WPSettings.dat",
)
# ext4 neighbours whose inode number stays a real signal.
INODE_CHECKED = ("/boot/vmlinuz", "/boot/firmware-old", "/boot/initrd.img")


def test_every_vfat_path_skips_its_inode_and_its_ext4_neighbours_do_not():
    conf = render_component_text("aide-scan", "98_aide_local.conf.j2", AIDE_LOCAL_VARS)
    assert [p for p in VFAT_PATHS if not _inode_skipped(p, conf)] == []
    assert [p for p in INODE_CHECKED if _inode_skipped(p, conf)] == []


def test_a_firmware_path_without_its_rule_is_flagged():
    efi_only = "/boot/efi/EFI/(ubuntu|BOOT)/[^/]+$ f Full-i\n"
    assert _inode_skipped("/boot/efi/EFI/ubuntu/grubx64.efi", efi_only)
    assert not _inode_skipped("/boot/firmware/README", efi_only)


# Debian's /etc/aide/aide.conf: `@@x_include /etc/aide/aide.conf.d ^[a-zA-Z0-9_-]+$`. A
# conf.d file whose name fails this is skipped silently, and `aide --config-check` still passes.
DEBIAN_INCLUDE_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


AIDE_CONF_D = "/etc/aide/aide.conf.d/"


def _aide_local_dest(host: str) -> str:
    dests = [path for path in package(host).files if path.startswith(AIDE_CONF_D)]
    assert len(dests) == 1, (
        f"{host} ships {dests} under {AIDE_CONF_D}, not one exclusions file"
    )
    return dests[0]


def test_the_include_pattern_skips_a_dotted_name_and_loads_a_plain_one():
    assert not DEBIAN_INCLUDE_NAME.match("98_aide_local.conf")
    assert DEBIAN_INCLUDE_NAME.match("98_aide_local")


def test_the_exclusions_deploy_under_a_name_aide_loads():
    """From 2026-05-31 to 2026-10-20 the file was `98_aide_local.conf` and no rule in it ran."""
    for host in AIDE_HOSTS:
        dest = _aide_local_dest(host)
        assert DEBIAN_INCLUDE_NAME.match(dest.rsplit("/", 1)[1]), (
            f"{host}: {dest} fails aide.conf's include pattern, so AIDE never reads it"
        )


PUSH_MARKERS = ("kuma_push", "kuma-push-lib", "KUMA_", "PUSH_TOKEN", "api/push")


def push_markers(body: str) -> list[str]:
    """The Kuma push plumbing a rendered script still carries, or nothing."""
    return [marker for marker in PUSH_MARKERS if marker in body]


def test_the_rendered_script_carries_no_kuma_push():
    """M6 batch B9's Cutover-2: the push, its token read and its library are gone."""
    assert push_markers(render_shell_script(*SCRIPT)) == []
    # The rejecting half: a body that still sources the library or reads the token is flagged.
    assert push_markers("source /usr/lib/homelab/kuma-push-lib.sh") == ["kuma-push-lib"]
    assert push_markers('AIDE_PUSH_TOKEN="x"') == ["PUSH_TOKEN"]


def test_the_check_logs_its_verdict_and_still_runs_without_a_credential(tmp_path):
    """No unit loads a credential, so a run has no token and still reports."""
    _, verdict, _, ran = _run_check(tmp_path, CLEAN_REPORT, rc=0)
    assert ran, "aide did not run"
    assert verdict is not None and verdict[0] == "up", verdict


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


def test_the_timer_runs_a_slot_it_missed():
    """daniel-pi's timer was stopped over the 2026-11-09 03:00 slot and never ran it (#7920)."""
    for host in AIDE_HOSTS:
        assert re.search(r"^Persistent=true$", shipped_text(host, TIMER), re.M), host


def test_the_push_deadline_covers_one_week_and_not_two():
    """The producer is homelab-aide-check.timer (aide-scan component), weekly on Monday."""
    for host in AIDE_HOSTS:
        on_calendar = re.search(r"^OnCalendar=(.+)$", shipped_text(host, TIMER), re.M)
        assert on_calendar and re.fullmatch(
            r"Mon \*-\*-\* \d\d:\d\d:\d\d", on_calendar[1]
        ), (
            f"{host}: this test reads the timer as weekly; give it the new period if that changes"
        )
    body = render_shell_script("components", "aide-scan", "aide-check.sh.j2")
    [deadline] = re.findall(r"^\s*DEADLINE_S=(\d+)$", body, re.M)
    assert WEEK_S < int(deadline) < 2 * WEEK_S, (
        f"DEADLINE_S {deadline}s must exceed the timer's weekly period, and stay "
        "under two so a second missed run cannot pass unreported"
    )
