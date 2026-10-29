"""`place clean` removes a squash-merged batch whose worktree carries a pre-#6552 script (#6571).

#6552 taught `clean-one` to count a tip inside a merged PR's head as landed, but the remote leg
ran the BATCH WORKTREE's own `fanout_place.py`. That copy is frozen at the batch's launch, so
every batch started before #6552 kept judging by the old ladder and `clean` kept them with
"not merged into origin/master" although their PRs had merged. These tests drive the real
`place clean` entry over a saved manifest, run the command it builds under bash, and let the
real `clean-one` run against a scratch repo with a stub `gh` on PATH.

Run: uv run pytest scripts/dev/tests/test_fanout_clean_landed.py
"""

import dataclasses
import sys
from pathlib import Path

from fanout_lib import clean as clean_mod
from fanout_lib.manifest import Batch, Manifest, path as manifest_path, save
from fanout_lib.place import main
from fanout_lib.target import PROJECT_DIR_ENV, SERVER_CHECKOUT
from lib.git_testing import commit, git, init_repo, scrubbed_env
from lib.proc_testing import fake_bin, path_with, run
from _fanout_fakes import fake_tools

BRANCH = "worktree-fanout-x"
UNIT = "fanout-x"
# The `clean-one` a batch launched before #6552 carries in its own worktree.
STALE_SCRIPT = 'import sys\nprint("kept:", sys.argv[2], sys.argv[3], "not merged into origin/master")\n'
SCRIPTS = Path(clean_mod.__file__).resolve().parents[2]


def _scratch(tmp_path):
    """A primary checkout, the real `scripts/` linked into it, and a batch worktree on a branch.

    The branch holds a change master lacks, so no local layer of `is_merged` can call it
    landed. The worktree commits the stale `fanout_place.py`; the primary has the current one.
    """
    origin = init_repo(tmp_path / "origin.git", bare=True)
    primary = init_repo(tmp_path / "primary", initial_commit="init")
    git(primary, "remote", "add", "origin", str(origin))
    git(primary, "push", "-q", "origin", "master")
    (primary / "scripts").symlink_to(SCRIPTS)
    worktree = tmp_path / "w"
    git(primary, "worktree", "add", "-q", "-b", BRANCH, str(worktree))
    commit(
        worktree,
        "work",
        **{"feature.txt": "unlanded\n", "scripts/dev/fanout_place.py": STALE_SCRIPT},
    )
    git(primary, "worktree", "lock", "--reason", UNIT, str(worktree))
    return primary, worktree


def _stubs(tmp_path, merged_head):
    """`gh` says a merged PR's head is `merged_head` and that the tip is inside it.

    That is the shape of a squash-merged batch `land.sh` merged master into: the merged head
    is not the local tip, so only the compare can vouch for it. `uv` drops its flags and runs
    the interpreter this suite runs, and `systemctl` reports the unit inactive.
    """
    return fake_bin(
        tmp_path / "bin",
        systemctl='#!/bin/sh\ncase "$2" in is-active) exit 3 ;; esac\nexit 0\n',
        gh=(
            '#!/bin/sh\ncase "$1" in\n'
            f"  pr) echo {merged_head} ;;\n  api) echo ahead ;;\nesac\n"
        ),
        uv=(
            '#!/bin/sh\nwhile [ "$1" != python ]; do shift; done; shift\n'
            f'exec {sys.executable} "$@"\n'
        ),
    )


def _tools_running_for_real(primary, stub_bin):
    """Tools whose `run` executes the command it is given under bash, aimed at `primary`."""
    env = scrubbed_env(**{PROJECT_DIR_ENV: str(primary)})
    env["PATH"] = path_with(stub_bin, env=env)

    def run_here(_host, command, timeout, stdin=None):
        command = command.replace(SERVER_CHECKOUT, str(primary))
        assert SERVER_CHECKOUT not in command  # never aimed at the shared checkout
        return run(["bash", "-c", command], env=env, timeout=timeout)

    tools, _calls = fake_tools()
    return dataclasses.replace(tools, run=run_here)


def test_a_squash_merged_batch_is_removed_though_its_worktree_script_predates_the_fix(
    tmp_path, capsys
):
    primary, worktree = _scratch(tmp_path)
    manifest = Manifest(
        "20261029T000000Z",
        "o",
        [Batch("x", "daniel-server", str(worktree), BRANCH, UNIT, [1], "t")],
    )
    save(manifest, root=tmp_path)
    tools = _tools_running_for_real(primary, _stubs(tmp_path, "f" * 40))

    code = main(["clean", manifest.run_id, "--manifest-root", str(tmp_path)], tools)

    out = capsys.readouterr().out
    assert code == 0, out
    assert f"x on daniel-server: removed: {worktree}" in out
    assert not worktree.exists()
    assert BRANCH not in git(primary, "branch", "--list").stdout
    assert not manifest_path(manifest.run_id, tmp_path).exists()
