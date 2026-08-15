"""Guard: prek's ansible-lint scope must never hand ansible-lint a file `.ansible-lint` excludes.

`exclude_paths` in `.ansible-lint` is honoured when ansible-lint WALKS the tree, and ignored for
files named explicitly on the command line. Verified 2026-08-14: `ansible-lint -c .ansible-lint
.github/workflows/ci.yml` lints that file happily, reporting "1 files processed", despite
`.github/` sitting in exclude_paths.

That did not matter while the prek hook ran with `pass_filenames = false` and let ansible-lint do
its own walking. It matters now: the hook passes filenames, so prek's `files`/`exclude` regexes
are the ONLY thing keeping an excluded file out of the lint. Two lists, one intent, and drift
between them is silent in the direction that hurts — widen `files` (or drop an `exclude`) and
excluded files start being linted, producing failures whose cause points nowhere near the change
that caused them. The vendored `ansible/collections/` changelog YAMLs are the live example: they
trip `yaml[indentation]`, and today they stay out only because they are gitignored AND the regex
is scoped to what prek tracks.

This is the same class of drift test_prek_pytest_always_runs.py addresses, though that one
resolves it by removing the hand-maintained regex rather than keeping it honest — the `pytest`
hook takes no filenames, so it could drop its `files` gate entirely. ansible-lint genuinely
lints the files it is given, so its regex has to stay and has to be checked.
"""

import fnmatch
import re
import subprocess
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ansible_lint_hook() -> dict:
    data = tomllib.loads((REPO_ROOT / "prek.toml").read_text())
    for repo in data.get("repos", []):
        for hook in repo.get("hooks", []):
            if hook.get("id") == "ansible-lint":
                return hook
    raise AssertionError("no `ansible-lint` hook found in prek.toml")


def _exclude_paths() -> list[str]:
    data = yaml.safe_load((REPO_ROOT / ".ansible-lint").read_text())
    return data.get("exclude_paths", [])


def _tracked_files() -> list[str]:
    """The candidate set is what prek can pass, and prek only ever passes TRACKED files."""
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.splitlines()


def _is_excluded(path: str, entries: list[str]) -> bool:
    for entry in entries:
        if "*" in entry:
            if fnmatch.fnmatch(path, entry):
                return True
        elif path == entry or path.startswith(entry.rstrip("/") + "/"):
            return True
    return False


def test_prek_never_passes_a_file_ansible_lint_excludes() -> None:
    hook = _ansible_lint_hook()
    files_re = re.compile(hook["files"])
    exclude_re = re.compile(hook["exclude"]) if hook.get("exclude") else None
    excluded = _exclude_paths()

    passed = [
        f
        for f in _tracked_files()
        if files_re.search(f) and not (exclude_re and exclude_re.search(f))
    ]
    assert passed, (
        "prek's ansible-lint `files` regex matches no tracked file at all — the hook would "
        "run with an empty list, and ansible-lint with no arguments walks the whole tree"
    )

    leaked = sorted(f for f in passed if _is_excluded(f, excluded))
    assert not leaked, (
        "prek would hand ansible-lint files that `.ansible-lint` excludes, and exclude_paths "
        "are ignored for explicitly-passed files:\n  "
        + "\n  ".join(leaked)
        + "\nWiden the hook's `exclude` in prek.toml (or narrow its `files`) to match."
    )


def test_ansible_lint_hook_passes_filenames_serially() -> None:
    """The two settings the scope guard above depends on, asserted so they can't quietly revert.

    `pass_filenames` is what makes prek's regexes load-bearing at all. `require_serial` is what
    stops prek sharding the list across parallel invocations — measured 2026-08-14 at 16 batches
    for a full run, each re-running the hook's `ansible-galaxy install` and paying ansible-lint's
    startup again, which made `--all-files` slower (43.8s) than the walk it replaced (37.0s).
    """
    hook = _ansible_lint_hook()
    assert hook.get("pass_filenames") is True, (
        "the ansible-lint hook must keep pass_filenames = true; without it ansible-lint walks "
        "the tree and the changed-files speedup in .github/workflows/ci.yml silently does nothing"
    )
    assert hook.get("require_serial") is True, (
        "the ansible-lint hook must keep require_serial = true, or prek shards the filenames "
        "across invocations and re-pays the galaxy install + startup for every batch"
    )


def test_ansible_lint_entry_forwards_its_filenames() -> None:
    """`bash -c '<script>'` puts appended filenames in the SCRIPT's positional params.

    Without the `--` sentinel and `"$@"`, prek's filenames never reach ansible-lint: it would be
    invoked bare, walk the whole tree, and still report Passed — slow and silently not what the
    workflow asked for. Same shape as the bash-syntax-check hook's entry.
    """
    entry = _ansible_lint_hook()["entry"]
    assert '"$@"' in entry, (
        f'ansible-lint entry must forward its filenames with "$@": {entry!r}'
    )
    assert entry.rstrip().endswith("--"), (
        f'ansible-lint entry must end with the `--` sentinel so filenames become "$@" rather '
        f"than the script's own $0/$1: {entry!r}"
    )
