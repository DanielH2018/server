"""homelab-check-lib.sh: the shared host-check library M6's batch B0 installs on every host.

The plan is docs/target-architecture/monitoring/m6-retire-kuma.md, *B0, B5, B7 and B8*. The
library carries `write_check_textfile` (M3's atomic write) and the three helpers moved from
kuma-push-lib.sh. Consumers render under other roles' tags, so two things have to hold or a
tag-scoped run leaves a host without the file: the copy task and its stamp are `tags: [always]`,
and every consumer sources the library with a failing guard instead of skipping silently.
"""

import re

from _helpers import ANSIBLE, load_tasks, task_named
from lib.proc_testing import run

LIB = ANSIBLE / "roles/setup/initial_setup/files/homelab-check-lib.sh"
CRONS = ANSIBLE / "roles/setup/initial_setup/tasks/crons.yml"
LIB_PATH = "/usr/local/lib/homelab-check-lib.sh"
SOURCE_LINE = re.compile(
    r"^\s*(?:source|\.)\s+" + re.escape(LIB_PATH) + r"(?P<rest>.*)$"
)


def _bash(tmp_path, body, textfile_dir=None):
    """Run `body` with the library sourced and TEXTFILE_DIR pointed at a temp directory."""
    textfile_dir = textfile_dir or tmp_path / "textfile"
    textfile_dir.mkdir(exist_ok=True)
    return run(
        ["bash", "-c", f"source {LIB}\n{body}"],
        env={"TEXTFILE_DIR": str(textfile_dir)},
    )


def test_write_check_textfile_writes_the_three_series_and_detail_lines_atomically(
    tmp_path,
):
    result = _bash(
        tmp_path,
        "write_check_textfile demo 1 1760000000 900 'detail{check=\"demo\"} 7'",
    )
    assert result.returncode == 0, result.stderr
    out = tmp_path / "textfile"
    assert sorted(p.name for p in out.iterdir()) == ["demo.prom"]
    prom = out / "demo.prom"
    assert prom.stat().st_mode & 0o777 == 0o644
    lines = prom.read_text().splitlines()
    assert 'homelab_check_success{check="demo"} 1' in lines
    assert 'homelab_check_last_run_timestamp_seconds{check="demo"} 1760000000' in lines
    assert 'homelab_check_deadline_seconds{check="demo"} 900' in lines
    assert 'detail{check="demo"} 7' in lines


def test_write_check_textfile_failure_returns_non_zero_and_leaves_no_file(tmp_path):
    missing = tmp_path / "no-such-dir"
    result = run(
        ["bash", "-c", f"source {LIB}\nwrite_check_textfile demo 1 1 1"],
        env={"TEXTFILE_DIR": str(missing)},
    )
    assert result.returncode != 0
    assert not missing.exists()


def test_reachout_verdict_pages_when_the_wan_answers_and_skips_when_it_does_not(
    tmp_path,
):
    # ACCEPT and REJECT halves: a stub curl that succeeds, then one that fails.
    probe = 'logger() { :; }\nreachout_verdict t https://a.example\necho "$REACHOUT_STATUS|$REACHOUT_NOTE"'
    answers = _bash(tmp_path, f"curl() {{ return 0; }}\n{probe}")
    refuses = _bash(tmp_path, f"curl() {{ return 7; }}\n{probe}")
    assert answers.stdout == "down|\n"
    assert refuses.stdout == "up|skipped: WAN unreachable — \n"


def test_boot_grace_active_fails_open_on_a_host_past_the_grace(tmp_path):
    # A grace of 0 can never exceed uptime, so the guard must let the check run.
    result = _bash(tmp_path, "logger() { :; }\nboot_grace_active 0 t")
    assert result.returncode == 1


def test_the_library_copy_task_and_its_stamp_are_tagged_always():
    tasks = load_tasks(CRONS)
    copy = task_named(tasks, "Deploy the shared host-check library")
    stamp = task_named(tasks, "Record the deployed shared host-check library")
    assert copy["tags"] == ["always"]
    assert stamp["tags"] == ["always"]
    assert copy["ansible.builtin.copy"]["dest"] == LIB_PATH


def _unguarded_sources(text):
    """Line numbers where the library is sourced without a failing `||` guard."""
    lines = text.splitlines()
    bad = []
    for i, line in enumerate(lines):
        m = SOURCE_LINE.match(line)
        if not m:
            continue
        guarded = "||" in m["rest"] and "exit 1" in "\n".join(lines[i : i + 5])
        if not guarded:
            bad.append(i + 1)
    return bad


def test_a_source_without_a_failing_guard_is_caught_and_a_guarded_one_is_not():
    unguarded = f"source {LIB_PATH}\n"
    guarded = f"source {LIB_PATH} || {{\n  logger -t x missing\n  exit 1\n}}\n"
    assert _unguarded_sources(unguarded) == [1]
    assert _unguarded_sources(guarded) == []


def test_every_rendered_script_sources_the_library_with_a_failing_guard():
    offenders = []
    for path in sorted((ANSIBLE / "roles").glob("**/*")):
        if path.suffix not in {".j2", ".sh"} or path == LIB:
            continue
        for lineno in _unguarded_sources(path.read_text()):
            offenders.append(f"{path.relative_to(ANSIBLE)}:{lineno}")
    assert not offenders, f"sourced without `|| exit 1`: {offenders}"
