"""Build a throwaway git repository for a test, with the inherited `GIT_*` environment gone.

WHY A SHARED MODULE. A test that runs `git -C <tmp_path> commit` under `prek`'s pytest hook
writes the REAL checkout: `git commit` exports `GIT_DIR` and `GIT_INDEX_FILE` to its hooks,
and git resolves both before `-C` or `cwd`. Test modules that each re-derive the scrub
disagree on what else goes in it — some pin `GIT_CONFIG_GLOBAL`, some do not, some set a commit
identity and some rely on the host's. A module that forgets one line passes on a developer box
and writes the primary repository's config under a hook.

`leakguard.py` strips `GIT_DIR`, `GIT_INDEX_FILE` and `GIT_WORK_TREE` from `os.environ` once
at plugin load, so it is the backstop for the whole suite. This module is the positive form:
it hands a caller an environment and a runner that are correct by construction, including the
two things leakguard cannot supply — a commit identity, and `GIT_CONFIG_GLOBAL` pointed at
the null device so this host's global SSH commit signing does not reach a scratch commit.

`lib.git.git` is the production runner and takes no `env` argument on purpose; its own
docstring sends a caller that commits to `subprocess` directly. That is what this module does,
and it is why the `git-and-gh-go-through-lib` row of
`ansible/tests/repo/test_census_rows_python.py` scopes its rule to production modules under
`scripts/`, outside `lib/` and every `tests/` directory.

Import it from any test, at any depth — `pyproject.toml` puts `scripts/` on `pythonpath`::

    from lib.git_testing import commit, init_repo

A test whose subject IS the environment handling does not use this. `scripts/lib/tests/
test_git.py` covers `lib.git`'s own scrub, and `ansible/tests/deploy/test_setup_drift_check.py`
pins `GIT_CONFIG_NOSYSTEM` and `GIT_TEST_ASSUME_DIFFERENT_OWNER` and asserts why each pin is
there. Both are named in the guard's exemption set with that reason.
"""

import os
import subprocess
from pathlib import Path

__all__ = [
    "commit",
    "git",
    "git_out",
    "init_repo",
    "scrub_process_git_env",
    "scrubbed_env",
]

# The identity every scratch commit is authored with. A fixed value rather than the host's, so
# a test asserting on an author line reads the same on CI and on a workstation.
AUTHOR_NAME = "t"
AUTHOR_EMAIL = "t@example.invalid"


def scrubbed_env(**overrides: str) -> dict[str, str]:
    """The real environment minus every `GIT_*` variable, plus a scratch commit identity.

    Every `GIT_*` name is dropped rather than the three hook variables alone: `GIT_AUTHOR_DATE`
    and `GIT_CONFIG_COUNT` redirect a scratch commit just as effectively, and a filter on the
    prefix cannot go stale the way a hand-kept list does.

    `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` both point at the null device, so neither this
    host's global SSH commit-signing config nor a system-wide `[user]` block reaches the
    scratch repository.

    Args:
        **overrides: Variables written after the scrub, for a test that needs one back.
            `test_setup_drift_check.py`'s `GIT_TEST_ASSUME_DIFFERENT_OWNER` is the shape.

    Returns:
        A fresh dict, safe for the caller to mutate.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = AUTHOR_NAME
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = AUTHOR_EMAIL
    env["GIT_CONFIG_GLOBAL"] = env["GIT_CONFIG_SYSTEM"] = os.devnull
    env.update(overrides)
    return env


def git(
    cwd: str | Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `git <args>` in `cwd` under `scrubbed_env()` and return the completed process.

    Args:
        cwd: The directory to run in. It alone decides which repository is read, which is the
            property the scrub exists to restore.
        check: Raise `CalledProcessError` on a non-zero exit. Pass `False` to read
            `returncode` yourself, as a `merge-base --is-ancestor` test does.
        env: A replacement environment, for a caller that built one through `scrubbed_env`
            overrides. Defaults to a plain `scrubbed_env()`.
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=scrubbed_env() if env is None else env,
        check=check,
        capture_output=True,
        text=True,
    )


def git_out(cwd: str | Path, *args: str, **kwargs) -> str:
    """`git(...).stdout` with surrounding whitespace removed."""
    return git(cwd, *args, **kwargs).stdout.strip()


def init_repo(
    path: Path,
    *,
    branch: str = "master",
    bare: bool = False,
    initial_commit: str | None = None,
) -> Path:
    """Create a repository at `path` and return it.

    Args:
        path: Where the repository goes. Created if it does not exist.
        branch: The initial branch. `master` matches this repo's default, so a test asserting
            on a branch name does not have to say which git version it ran under.
        bare: Build a bare repository, for a test that needs a push target.
        initial_commit: When given, make an EMPTY commit carrying this message. A repository
            with no commit at all is its own case — `rev-parse HEAD` fails there — so the
            commit is opt-in rather than the default.
    """
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", *(["--bare"] if bare else []), "-b", branch)
    if initial_commit is not None:
        git(
            path, "commit", "-q", "--allow-empty", "-m", initial_commit, "--no-gpg-sign"
        )
    return path


def commit(repo: Path, message: str, **files: str | None) -> str:
    """Write (or delete, for `None`) each file, commit everything, and return the new SHA.

    A keyword name is the path relative to `repo`, so a nested file is passed through a dict:
    ``commit(repo, "msg", **{"a/b.txt": "x"})``.
    """
    for name, content in files.items():
        target = repo / name
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, "--no-gpg-sign")
    return git_out(repo, "rev-parse", "HEAD")


def scrub_process_git_env(monkeypatch) -> list[str]:
    """Drop every `GIT_*` variable from THIS process, and return the names dropped.

    For a test whose subject runs git in-process and builds its own environment, where passing
    `scrubbed_env()` down is not possible — `prune_worktrees.remove()` is the worked example.

    Args:
        monkeypatch: pytest's fixture, so the variables come back at teardown.
    """
    dropped = sorted(name for name in os.environ if name.startswith("GIT_"))
    for name in dropped:
        monkeypatch.delenv(name, raising=False)
    return dropped
