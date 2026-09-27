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
from pathlib import Path


import land_tags
from lib.render_guard import HOST_VARS_IN_TREE

# `wg-easy` is the real two-platform shape: a `platform: k8s` entry on daniel-box and a
# Compose entry on daniel-pi, which carries no `platform` key at all (`entry_platform`
# defaults it to `docker`). One tag name, two services, two hosts.
_BOX = (
    "containers_list:\n"
    "  - name: sonarr\n"
    "    platform: k8s\n"
    "  - name: wg-easy\n"
    "    platform: k8s\n"
)
_STAGE = "containers_list:\n  - name: sonarr\n    platform: k8s\n"
_PI = "containers_list:\n  - name: newpi\n  - name: wg-easy\n"


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


def test_a_caller_expanded_tag_routes_to_the_k8s_entry_alone(tmp_path):
    """CLEAN half of #2718: a `manifests` change reaches `wg-easy` through the k8s caller
    graph, so it must not also deploy the Pi's Compose service of that name."""
    shas = _repo(tmp_path)
    assert land_tags.landing_hosts_at(
        ["wg-easy"], shas[1], tmp_path, k8s_only=["wg-easy"]
    ) == {"daniel-box": ["wg-easy"]}


def test_the_same_tag_unrestricted_still_routes_to_both_hosts(tmp_path):
    """REJECTING half: the filter is opt-in per tag. A tag no caller expansion produced keeps
    reaching every host that declares it, because narrowing host routing is how issue #929
    (a landing that read `settled` while the Pi ran old code) comes back."""
    shas = _repo(tmp_path)
    assert land_tags.landing_hosts_at(["wg-easy"], shas[1], tmp_path) == {
        "daniel-box": ["wg-easy"],
        "daniel-pi": ["wg-easy"],
    }


def test_restricting_one_tag_leaves_the_others_routed_as_before(tmp_path):
    """The filter is per tag, not per host: `newpi` is Docker-only and stays on the Pi."""
    shas = _repo(tmp_path)
    assert land_tags.landing_hosts_at(
        ["newpi", "wg-easy"], shas[1], tmp_path, k8s_only=["wg-easy"]
    ) == {"daniel-box": ["wg-easy"], "daniel-pi": ["newpi"]}


def test_restricting_a_tag_with_no_k8s_entry_leaves_it_routed(tmp_path):
    """The fail-safe: `newpi` is declared on daniel-pi alone, so restricting it to k8s would
    route it to NO host -- and the landing then falls through to one local deploy that matches
    nothing and reads `settled` (issue #929). The restriction drops instead."""
    shas = _repo(tmp_path)
    assert land_tags.landing_hosts_at(
        ["newpi"], shas[1], tmp_path, k8s_only=["newpi"]
    ) == {"daniel-pi": ["newpi"]}
