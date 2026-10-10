"""Every shell entry point outside `scripts/` answers `--help` with exit 0.

The shell half of `test_entry_points_outside_scripts_answer_help.py`. It covers two kinds of
script: the tracked `.sh` files under `.claude/` and `ansible/`, and every `*.sh.j2` template a
role renders into a cron or timer script. A template runs as rendered, so the test renders it
through `_shell_render`, the render `validate/shell_templates.py` lints, and runs that.

THE CONVENTION. `-h` or `--help` anywhere in argv prints the script's header comment block and
exits 0, before any lock, network call or write. `scripts/deploy_tools/gitops_tick.sh` is the
model: one `awk` over `$0` prints the comment lines under the shebang.

WHY THE TEXT IS CHECKED BEFORE THE RUN. A script that has no help path does its real work when
this test runs it, and these are cron jobs: `docs-refresh` pushes a branch and `registry-gc`
deletes blobs. So a file whose text never mentions `--help` fails here unexecuted. The run
still decides, because a script can mention the flag and still do work before it looks.

Run: uv run pytest ansible/tests/repo/test_shell_entry_points_answer_help.py
"""

import os
import subprocess
from pathlib import Path

import pytest

from _helpers import REPO
from _shell_render import rendered_shell_texts

# Sourced, never executed. A sourced file's `$@` is its caller's argv, so a help check there
# would answer the caller's own `--help` with the library's comment and exit the caller.
EXEMPT = {
    "ansible/roles/setup/initial_setup/files/kuma-push-lib.sh",
    "ansible/roles/setup/initial_setup/files/setup-drift-lib.sh",
}

# Non-vacuity: one of each shape. A forced command, a hook launcher, a host cron template, a
# cluster-side template, and the template that pushes to GitHub.
MUST_FIND = frozenset(
    {
        "ansible/roles/k8s/pi-peer-backup/files/pi-peer-backup-shell.sh",
        ".claude/hooks/run-hook.sh",
        "setup/k3s/longhorn-backup-health.sh.j2",
        "k8s/crowdsec/crowdsec-prune-bouncers.sh.j2",
        "setup/initial_setup/docs-refresh.sh.j2",
    }
)

HELP_TIMEOUT_S = 30


def _tracked_scripts() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.sh"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=HELP_TIMEOUT_S,
    ).stdout.split("\0")
    return sorted(
        rel
        for rel in filter(None, listed)
        if not rel.startswith("scripts/") and "tests" not in Path(rel).parts
    )


TRACKED = _tracked_scripts()
TEMPLATES = {
    f"{plane}/{role}/{name}": text for plane, role, name, text in rendered_shell_texts()
}


def _help_failure(path: Path) -> str | None:
    """Why `bash path --help` is not a usable answer, or None when it is one."""
    if "--help" not in path.read_text(encoding="utf-8"):
        return "has no --help path; not run, because it would do its real work"
    try:
        r = subprocess.run(
            ["bash", str(path), "--help"],
            cwd=REPO,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=HELP_TIMEOUT_S,
            env={**os.environ},
        )
    except subprocess.TimeoutExpired:
        return (
            f"did not answer within {HELP_TIMEOUT_S}s: it does work before it looks at argv. "
            "Put the -h/--help case first, ahead of any flock or exec."
        )
    if r.returncode != 0:
        return f"exited {r.returncode}\nstdout: {r.stdout[-400:]}\nstderr: {r.stderr[-400:]}"
    if not r.stdout.strip():
        return "exited 0 and printed nothing to stdout"
    return None


def test_the_census_still_finds_the_scripts_it_is_measured_against():
    found = set(TRACKED) | set(TEMPLATES)
    missing = MUST_FIND - found
    assert not missing, f"the shell census lost {sorted(missing)}"
    assert len(TRACKED) >= 12, len(TRACKED)
    assert len(TEMPLATES) >= 30, len(TEMPLATES)


def test_every_exemption_is_still_a_tracked_script():
    stale = EXEMPT - set(TRACKED)
    assert not stale, f"exempt but no longer tracked, so drop it: {sorted(stale)}"


@pytest.mark.parametrize("rel", [r for r in TRACKED if r not in EXEMPT], ids=str)
def test_the_tracked_script_answers_help_with_exit_zero(rel):
    failure = _help_failure(REPO / rel)
    assert failure is None, f"{rel} --help {failure}"


@pytest.mark.parametrize("key", sorted(TEMPLATES), ids=str)
def test_the_rendered_template_answers_help_with_exit_zero(key, tmp_path):
    script = tmp_path / Path(key).name.removesuffix(".j2")
    script.write_text(TEMPLATES[key])
    failure = _help_failure(script)
    assert failure is None, f"{key} rendered, --help {failure}"


def test_a_script_with_no_help_path_is_flagged_without_running(tmp_path):
    marker = tmp_path / "ran"
    script = tmp_path / "does_work.sh"
    script.write_text(f"#!/bin/bash\n# Does work.\ntouch {marker}\n")
    assert _help_failure(script) is not None
    assert not marker.exists(), "the check ran a script that has no help path"


def test_a_script_that_works_before_its_help_case_is_flagged(tmp_path):
    script = tmp_path / "late.sh"
    script.write_text(
        "#!/bin/bash\n# Reads first.\nread -r line\nexit 1\n"
        'case "$1" in --help) echo usage; exit 0 ;; esac\n'
    )
    assert _help_failure(script) is not None


def test_a_script_that_answers_first_is_clean(tmp_path):
    script = tmp_path / "answers.sh"
    script.write_text(
        "#!/bin/bash\n# Prints a header.\n"
        'for arg in "$@"; do\n'
        "  case \"$arg\" in -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }'"
        ' "$0"; exit 0 ;; esac\n'
        "done\n"
        "read -r line\n"
    )
    assert _help_failure(script) is None
