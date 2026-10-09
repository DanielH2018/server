"""claude-cgroup-metrics.sh must emit the cgroup counters Prometheus needs for the Claude hosts.

claude-rc.service is bounded (MemoryHigh/MemorySwapMax) and user-1000.slice (SSH-started
sessions) is not, so each cgroup needs its own counters. This host sets
DefaultMemoryAccounting/CPUAccounting=yes, so the counters are populated on cgroupfs. The
script writes them as a node-exporter textfile gauge set (roles/k8s/node-exporter/CLAUDE.md
describes the `--collector.textfile.directory` hook).

Per this repo's red-proof rule, each behaviour below is a pair: one fixture it must read
correctly, one it must not choke on (a missing file, a missing textfile directory).

Run: uv run pytest ansible/tests/setup/test_claude_cgroup_metrics.py
"""

import subprocess
from pathlib import Path

import pytest
from _helpers import ANSIBLE
from lib.proc_testing import run

SCRIPT = (
    ANSIBLE / "roles" / "setup" / "claude_code" / "files" / "claude-cgroup-metrics.sh"
)

# claude-rc.service sits under the fleet's shared parent, user.slice, and
# claude_code_fleet_caps_enabled: false puts it back in system.slice. The script resolves
# between the two, so both paths are exercised below.
RC_UNDER_FLEET_PARENT = "user.slice/claude-rc.service"
RC_UNDER_SYSTEM_SLICE = "system.slice/claude-rc.service"

CGROUPS = {
    "fleet": "user.slice",
    "claude-rc": RC_UNDER_FLEET_PARENT,
    "user-1000-slice": "user.slice/user-1000.slice",
}


def _write_cgroup_fixture(cgroot: Path, rel: str) -> Path:
    d = cgroot / rel
    # exist_ok: the fleet parent (user.slice) and its two children are all fixtures here, so
    # whichever is written second finds its directory already made.
    d.mkdir(parents=True, exist_ok=True)
    (d / "memory.current").write_text("399777792\n")
    (d / "memory.swap.current").write_text("0\n")
    (d / "memory.events").write_text(
        "low 0\nhigh 3\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n"
    )
    (d / "memory.pressure").write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=291\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=291\n"
    )
    (d / "cpu.stat").write_text("usage_usec 67096514000\nnr_periods 0\n")
    (d / "pids.current").write_text("65\n")
    return d


def _run(cgroot: Path, textfile_dir: Path) -> subprocess.CompletedProcess:
    env = {
        "CGROOT": str(cgroot),
        "TEXTFILE_DIR": str(textfile_dir),
        "PATH": "/usr/bin:/bin",
    }
    return run(["bash", str(SCRIPT)], env=env, check=False)


@pytest.fixture()
def full_fixture(tmp_path: Path) -> tuple[Path, Path]:
    cgroot = tmp_path / "cgroup"
    for rel in CGROUPS.values():
        _write_cgroup_fixture(cgroot, rel)
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir()
    return cgroot, textfile_dir


def test_script_exists_and_task_deploys_it_executable() -> None:
    """The repo blob itself is 644 (matches every other `copy:`-deployed script here, e.g.
    b2-usage.sh) — `copy:` sets the live mode independent of the source's, so the executable
    bit has to come from the task, not the file."""
    assert SCRIPT.exists(), f"{SCRIPT} is missing"
    tasks = (
        ANSIBLE / "roles" / "setup" / "claude_code" / "tasks" / "main.yml"
    ).read_text()
    assert "src: claude-cgroup-metrics.sh" in tasks, (
        "no copy: task deploys claude-cgroup-metrics.sh"
    )
    assert 'mode: "0755"' in tasks, "the deployed script must be executable (mode 0755)"


def test_emits_every_metric_family_for_both_cgroups(
    full_fixture: tuple[Path, Path],
) -> None:
    cgroot, textfile_dir = full_fixture
    result = _run(cgroot, textfile_dir)
    assert result.returncode == 0, result.stderr

    out = (textfile_dir / "claude_cgroup.prom").read_text()
    families = [
        "claude_cgroup_memory_current_bytes",
        "claude_cgroup_memory_swap_current_bytes",
        "claude_cgroup_memory_events_total",
        "claude_cgroup_memory_pressure_stalled_usec_total",
        "claude_cgroup_cpu_usage_usec_total",
        "claude_cgroup_pids_current",
    ]
    for family in families:
        assert f"# TYPE {family}" in out, f"{family} missing its TYPE line: {out}"
        for label in CGROUPS:
            assert f'{family}{{cgroup="{label}"' in out, (
                f"{family} missing a sample for cgroup={label}: {out}"
            )

    # The counter the script exists for: memory.events' `high` field, which counts every time
    # MemoryHigh throttled the cgroup.
    assert 'claude_cgroup_memory_events_total{cgroup="claude-rc",event="high"} 3' in out


def test_output_is_valid_prometheus_text_format(
    full_fixture: tuple[Path, Path],
) -> None:
    """The rejecting half of the family/label pair above: catch a malformed line, not just a
    missing one. Every non-comment, non-blank line must be `name{labels} value`."""
    cgroot, textfile_dir = full_fixture
    _run(cgroot, textfile_dir)
    out = (textfile_dir / "claude_cgroup.prom").read_text()
    for line in out.splitlines():
        if not line or line.startswith("#"):
            continue
        name_labels, _, value = line.rpartition(" ")
        assert name_labels and value, f"malformed metric line: {line!r}"
        float(value)  # raises if not a bare number


def test_missing_cgroup_directory_is_skipped_not_fatal(tmp_path: Path) -> None:
    """Rejecting half: a cgroup that does not exist (e.g. claude-rc.service stopped) must not
    crash the whole run — the other cgroup's metrics still need to land."""
    cgroot = tmp_path / "cgroup"
    _write_cgroup_fixture(cgroot, CGROUPS["user-1000-slice"])
    # claude-rc.service is deliberately absent from BOTH slices it can live in.
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir()

    result = _run(cgroot, textfile_dir)
    assert result.returncode == 0, result.stderr
    out = (textfile_dir / "claude_cgroup.prom").read_text()
    assert 'cgroup="user-1000-slice"' in out
    assert 'cgroup="claude-rc"' not in out


def test_claude_rc_is_found_under_the_fleet_parent_slice(tmp_path: Path) -> None:
    """The accepting half of the path resolution: the unit's Slice= line puts its cgroup in
    user.slice, so a hardcoded system.slice path would emit nothing for this plane while the
    scrape stayed green."""
    cgroot = tmp_path / "cgroup"
    _write_cgroup_fixture(cgroot, RC_UNDER_FLEET_PARENT)
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir()

    assert _run(cgroot, textfile_dir).returncode == 0
    out = (textfile_dir / "claude_cgroup.prom").read_text()
    assert 'claude_cgroup_memory_current_bytes{cgroup="claude-rc"}' in out, (
        f"claude-rc.service under {RC_UNDER_FLEET_PARENT} was not found: {out}"
    )


def test_claude_rc_is_still_found_under_system_slice(tmp_path: Path) -> None:
    """The rejecting half, and the rollback direction: claude_code_fleet_caps_enabled: false
    returns the unit to system.slice, so resolving only the user.slice path would lose the plane
    exactly when the shared parent has been turned off."""
    cgroot = tmp_path / "cgroup"
    _write_cgroup_fixture(cgroot, RC_UNDER_SYSTEM_SLICE)
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir()

    assert _run(cgroot, textfile_dir).returncode == 0
    out = (textfile_dir / "claude_cgroup.prom").read_text()
    assert 'claude_cgroup_memory_current_bytes{cgroup="claude-rc"}' in out, (
        f"claude-rc.service under {RC_UNDER_SYSTEM_SLICE} was not found: {out}"
    )


def test_missing_textfile_directory_exits_clean(tmp_path: Path) -> None:
    """Matches the b2-usage.sh guard: a host without the textfile hook (or before
    node-exporter is deployed there) must not fail the timer, just produce nothing."""
    cgroot = tmp_path / "cgroup"
    for rel in CGROUPS.values():
        _write_cgroup_fixture(cgroot, rel)
    missing_textfile_dir = tmp_path / "does-not-exist"

    result = _run(cgroot, missing_textfile_dir)
    assert result.returncode == 0, result.stderr
    assert not missing_textfile_dir.exists()


def test_output_file_is_world_readable(full_fixture: tuple[Path, Path]) -> None:
    """node-exporter's container reads this file as a non-root user; mktemp defaults to 0600,
    which reads as node_textfile_scrape_error=1 rather than a clean skip."""
    cgroot, textfile_dir = full_fixture
    _run(cgroot, textfile_dir)
    mode = (textfile_dir / "claude_cgroup.prom").stat().st_mode & 0o777
    assert mode == 0o644, f"expected mode 644, got {oct(mode)}"


def _proc(procroot: Path, pid: int, name: str, uid: int) -> None:
    """One /proc/<pid>/status in the kernel's layout: Name before Uid, real uid first."""
    d = procroot / str(pid)
    d.mkdir(parents=True)
    (d / "status").write_text(
        f"Name:\t{name}\nUmask:\t0002\nState:\tS (sleeping)\n"
        f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
    )


def _run_watching(tmp_path: Path, procroot: Path, uids: str) -> str:
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir()
    env = {
        "CGROOT": str(tmp_path / "no-cgroup"),
        "PROCROOT": str(procroot),
        "TEXTFILE_DIR": str(textfile_dir),
        "CLAUDE_WATCH_UIDS": uids,
        "PATH": "/usr/bin:/bin",
    }
    result = run(["bash", str(SCRIPT)], env=env, check=False)
    assert result.returncode == 0, result.stderr
    return (textfile_dir / "claude_cgroup.prom").read_text()


def test_a_watched_uid_running_claude_is_flagged(tmp_path: Path) -> None:
    """Slice 6: a `claude` process whose real uid is watched is counted. A `claude` of another
    uid and a non-claude process of the watched uid are not."""
    procroot = tmp_path / "proc"
    _proc(procroot, 10, "claude", 1000)
    _proc(procroot, 11, "claude", 1000)
    _proc(procroot, 12, "claude", 996)
    _proc(procroot, 13, "bash", 1000)
    out = _run_watching(tmp_path, procroot, "1000")
    assert 'claude_uid_processes{uid="1000"} 2' in out, out


def test_a_process_exiting_mid_scan_does_not_zero_the_count(tmp_path: Path) -> None:
    """A status path the glob matched but nothing can open, as when the process exits between
    the two. gawk aborts on it, which read every other `claude` as 0 on the live host."""
    procroot = tmp_path / "proc"
    (procroot / "5").mkdir(parents=True)
    (procroot / "5" / "status").symlink_to(tmp_path / "gone")
    _proc(procroot, 10, "claude", 1000)
    out = _run_watching(tmp_path, procroot, "1000")
    assert 'claude_uid_processes{uid="1000"} 1' in out, out


def test_a_watched_uid_running_no_claude_is_clean_and_still_reported(
    tmp_path: Path,
) -> None:
    """The quiet uid gets an explicit 0, so the series reads as checked rather than absent."""
    procroot = tmp_path / "proc"
    _proc(procroot, 12, "claude", 996)
    _proc(procroot, 13, "claude-helper", 1000)
    out = _run_watching(tmp_path, procroot, "1000 4242")
    assert 'claude_uid_processes{uid="1000"} 0' in out, out
    assert 'claude_uid_processes{uid="4242"} 0' in out, out


def test_an_empty_watch_list_writes_no_uid_series(tmp_path: Path) -> None:
    """The way out: on daniel-server, or any host with an empty list, nothing is judged."""
    procroot = tmp_path / "proc"
    _proc(procroot, 10, "claude", 1000)
    out = _run_watching(tmp_path, procroot, "")
    assert "claude_uid_processes" not in out, out
