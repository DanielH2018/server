"""claude-cgroup-metrics.sh must actually emit the counters issue #1238 asks for.

claude-rc.service is bounded (MemoryHigh/MemorySwapMax) and user-1000.slice (SSH-started
sessions, #1213) is not, but neither cgroup's counters reached Prometheus before this script:
this host sets DefaultMemoryAccounting/CPUAccounting=yes, so the counters were already free and
populated on cgroupfs, and nothing read them. The script writes them as a node-exporter textfile
gauge set (roles/k8s/node-exporter/CLAUDE.md: the `--collector.textfile.directory` hook was "an
empty hook, not yet used").

Per this repo's red-proof rule, each behaviour below is a pair: one fixture it must read
correctly, one it must not choke on (a missing file, a missing textfile directory).

Run: uv run pytest ansible/tests/setup/test_claude_cgroup_metrics.py
"""

import subprocess
from pathlib import Path

import pytest
from _helpers import ANSIBLE

SCRIPT = (
    ANSIBLE / "roles" / "setup" / "claude_code" / "files" / "claude-cgroup-metrics.sh"
)

CGROUPS = {
    "claude-rc": "system.slice/claude-rc.service",
    "user-1000-slice": "user.slice/user-1000.slice",
}


def _write_cgroup_fixture(cgroot: Path, rel: str) -> Path:
    d = cgroot / rel
    d.mkdir(parents=True)
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
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


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

    # The one counter #1238 names specifically: memory.events' `high` field, which counts
    # every time MemoryHigh throttled the cgroup — the number that would have narrated the
    # 2026-09-05 stall as it happened.
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
    # system.slice/claude-rc.service is deliberately absent.
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir()

    result = _run(cgroot, textfile_dir)
    assert result.returncode == 0, result.stderr
    out = (textfile_dir / "claude_cgroup.prom").read_text()
    assert 'cgroup="user-1000-slice"' in out
    assert 'cgroup="claude-rc"' not in out


def test_missing_textfile_directory_exits_clean(tmp_path: Path) -> None:
    """Matches the kopia-era b2-usage.sh guard: a host without the textfile hook (or before
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
