"""The root `.gitignore` admits `hosts/`, the source tree of the host packages (H3).

`.gitignore` denies every root path it does not re-allow. A file under a denied root stages
nothing and lands nothing, through a green CI and a clean merge.

Run: uv run pytest hosts/lib/tests
"""

from lib.proc_testing import run
from lib.repo_paths import REPO


def _ignored(path: str) -> bool:
    # --no-index reads the rules alone, so a path that is already tracked is still judged.
    return (
        run(["git", "check-ignore", "-q", "--no-index", path], cwd=REPO).returncode == 0
    )


def test_a_path_under_hosts_is_clean():
    assert not _ignored("hosts/daniel-pi/package.yml")


def test_a_root_path_the_gitignore_does_not_admit_is_flagged():
    assert _ignored("no-such-root/daniel-pi/package.yml")
