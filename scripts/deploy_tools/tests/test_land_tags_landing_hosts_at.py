"""`land_tags.landing_hosts_at`: the per-host split read at the merge commit (issue #1839).

`deploy_by_host` used to route tags through `deploy_tags.py hosts` against the primary
checkout, whatever `deploy.sh --at` rendered. A PR adding a Pi role and its containers_list
entry together declares the role on daniel-pi in no checkout until the tick fast-forwards, so
its first landing ran deploy.sh locally without `-e target=daniel-pi` and failed closed at the
health gate. Kept out of test_land_tags.py, which sits at its line cap.

Run: uv run pytest scripts/deploy_tools/tests/test_land_tags_landing_hosts_at.py
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import land_tags
from lib.render_guard import HOST_VARS_IN_TREE

_BOX = "containers_list:\n  - name: sonarr\n    platform: k8s\n"
_STAGE = "containers_list:\n  - name: sonarr\n    platform: k8s\n"
_PI = "containers_list:\n  - name: newpi\n"


def _repo(tmp_path: Path) -> list[str]:
    """Two commits: the box alone, then the Pi and the staging guest added beside it."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {
        "GIT_AUTHOR_NAME": "t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }

    def run(*args: str) -> str:
        return subprocess.run(
            args, cwd=tmp_path, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    run("git", "init", "-q", "-b", "master")
    host_vars = tmp_path / HOST_VARS_IN_TREE
    host_vars.mkdir(parents=True)
    shas = []
    for files in (
        {"daniel-box.yml": _BOX},
        {"daniel-pi.yml": _PI, "daniel-stage.yml": _STAGE},
    ):
        for name, text in files.items():
            (host_vars / name).write_text(text)
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", "c", "--no-gpg-sign")
        shas.append(run("git", "rev-parse", "HEAD"))
    return shas


def test_a_pi_role_added_at_the_merge_commit_routes_to_the_pi(tmp_path):
    """CLEAN half: read at the commit that adds it, the new role is declared on daniel-pi."""
    shas = _repo(tmp_path)
    assert land_tags.landing_hosts_at(["newpi", "sonarr"], shas[1], tmp_path) == {
        "daniel-box": ["sonarr"],
        "daniel-pi": ["newpi"],
    }


def test_the_same_role_is_on_no_host_at_the_commit_before(tmp_path):
    """The answer moves with the ref, or reading at one proves nothing."""
    shas = _repo(tmp_path)
    assert land_tags.landing_hosts_at(["newpi"], shas[0], tmp_path) == {}


def test_the_staging_guest_is_dropped_the_way_the_tree_read_drops_it(tmp_path):
    """Issue #935's exclusion holds on this path too: land.sh never deploys to daniel-stage."""
    shas = _repo(tmp_path)
    assert land_tags.landing_hosts_at(["sonarr"], shas[1], tmp_path) == {
        "daniel-box": ["sonarr"]
    }


def test_an_unreadable_ref_is_none_not_no_hosts(tmp_path):
    """REJECTING half: `{}` means "no host declares it", which is one local deploy; None sends
    the caller back to the primary read (issue #1331's damage rule)."""
    _repo(tmp_path)
    assert land_tags.landing_hosts_at(["sonarr"], "deadbeefdeadbeef", tmp_path) is None
