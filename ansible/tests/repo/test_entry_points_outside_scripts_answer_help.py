"""Every Python entry point outside `scripts/` answers `--help` with exit 0.

`scripts/lib/tests/test_entry_points_answer_help.py` holds `scripts/` to this. This test
covers the rest of the tree: the Claude hooks, the scripts a role ships from its `files/`, and
`evals/`. An entry point here is any tracked `.py` with an `if __name__ == "__main__"` guard
that is not a test module.

WHY A SUBPROCESS AND NOT A STATIC CHECK. The answer is what matters, not the presence of an
argparse call. `ical_proxy.py` started its Flask server when asked for `--help`, and a hook
that reads its payload before it looks at argv waits on stdin forever. A grep for `argparse`
or `--help` passes on both.

HOW A SCRIPT RUNS HERE. Under this interpreter, from the repo root, with stdin closed. A role
script cannot import `scripts/lib/cli_help`, because a role ships only its own `files/`, so
each one checks argv itself. Some import a module that another role's `files/` owns and the
deploy copies beside them (`host_lib`, `bridge.common`, the `gitops_*` trio). `SHIPPED_BESIDE`
puts those source directories on `PYTHONPATH` to stand in for that copy.

Run: uv run pytest ansible/tests/repo/test_entry_points_outside_scripts_answer_help.py
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from _helpers import REPO

# The `files/` directories whose modules a deploy copies beside a consumer in another role.
SHIPPED_BESIDE = (
    REPO / "ansible/roles/setup/common/files",
    REPO / "ansible/roles/setup/gitops_deploy/files",
    REPO / "ansible/roles/k8s/monitor-bridge/files",
)

# Non-vacuity: one of each shape the census must keep. A hook, a role script that takes
# positional arguments, a cluster daemon, a host cron, and the Flask app that served instead
# of answering.
MUST_FIND = frozenset(
    {
        ".claude/hooks/block-footguns.py",
        "ansible/roles/k8s/manifests/files/secret_hmac.py",
        "ansible/roles/k8s/monitor-bridge/files/check.py",
        "ansible/roles/setup/gitops_deploy/files/gitops_deploy.py",
        "ansible/roles/k8s/ical-proxy/files/ical_proxy.py",
    }
)

HELP_TIMEOUT_S = 30

# Entry points that answer `--help` where they run but cannot here, each with the reason.
EXEMPT = {
    # Vendored byte-for-byte from upstream and pinned by sha256 in
    # ansible/tests/services/test_karakeep_time_tagger_script.py, so a guard would break the pin.
    # It ends in `fire.Fire(main)`, which answers --help in the tagger image; the repo env has
    # no `fire`, so the import fails here first.
    "ansible/roles/k8s/karakeep/files/karakeep-time-tagger.py",
}


def _entry_points() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=HELP_TIMEOUT_S,
    ).stdout.split("\0")
    found = []
    for rel in filter(None, listed):
        path = Path(rel)
        if rel.startswith("scripts/") or "tests" in path.parts:
            continue
        if path.name.startswith("test_") or path.name == "conftest.py":
            continue
        text = (REPO / rel).read_text(encoding="utf-8")
        if 'if __name__ == "__main__"' in text:
            found.append(rel)
    return sorted(found)


ENTRY_POINTS = _entry_points()


def _help_failure(path: Path) -> str | None:
    """Why `path --help` is not a usable answer, or None when it is one."""
    text = path.read_text(encoding="utf-8")
    if "--help" not in text and "argparse" not in text:
        # A script with no help path would do its real work under this run: the host crons
        # here call cscli, read their config and tick the deployer. argparse answers --help
        # without the file spelling the flag out.
        return "has no --help path; not run, because it would do its real work"
    pythonpath = os.pathsep.join(str(p) for p in (path.parent, *SHIPPED_BESIDE))
    try:
        r = subprocess.run(
            [sys.executable, str(path), "--help"],
            cwd=REPO,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=HELP_TIMEOUT_S,
            env={**os.environ, "PYTHONPATH": pythonpath},
        )
    except subprocess.TimeoutExpired:
        return (
            f"did not answer within {HELP_TIMEOUT_S}s: it does work before it looks at argv. "
            "Check for -h/--help first in the `__main__` block."
        )
    if r.returncode != 0:
        return f"exited {r.returncode}\nstdout: {r.stdout[-400:]}\nstderr: {r.stderr[-400:]}"
    if not r.stdout.strip():
        return "exited 0 and printed nothing to stdout"
    return None


def test_the_census_still_finds_the_entry_points_it_is_measured_against():
    missing = MUST_FIND - set(ENTRY_POINTS)
    assert not missing, f"the entry-point census lost {sorted(missing)}"
    assert len(ENTRY_POINTS) >= 40, len(ENTRY_POINTS)


def test_every_exemption_is_still_an_entry_point():
    stale = EXEMPT - set(ENTRY_POINTS)
    assert not stale, (
        f"exempt but no longer an entry point, so drop it: {sorted(stale)}"
    )


@pytest.mark.parametrize("rel", [r for r in ENTRY_POINTS if r not in EXEMPT], ids=str)
def test_the_entry_point_answers_help_with_exit_zero(rel):
    failure = _help_failure(REPO / rel)
    assert failure is None, f"{rel} --help {failure}"


def test_a_script_that_reads_stdin_before_argv_is_flagged(tmp_path):
    script = tmp_path / "reads_stdin.py"
    script.write_text(
        '"""Reads a payload."""\nimport sys\n\nif __name__ == "__main__":\n'
        "    sys.exit(0 if sys.stdin.read() else 1)\n"
    )
    assert _help_failure(script) is not None


def test_a_script_with_no_help_path_is_flagged_without_running(tmp_path):
    marker = tmp_path / "ran"
    script = tmp_path / "does_work.py"
    script.write_text(
        f'"""Does work."""\nimport pathlib\n\npathlib.Path("{marker}").touch()\n'
    )
    assert _help_failure(script) is not None
    assert not marker.exists(), "the check ran a script that has no help path"


def test_a_script_that_checks_argv_first_is_clean(tmp_path):
    script = tmp_path / "answers.py"
    script.write_text(
        '"""Reads a payload."""\nimport sys\n\nif __name__ == "__main__":\n'
        '    if any(a in ("-h", "--help") for a in sys.argv[1:]):\n'
        "        print(__doc__)\n        sys.exit(0)\n"
        "    sys.exit(0 if sys.stdin.read() else 1)\n"
    )
    assert _help_failure(script) is None
