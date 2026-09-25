"""The throwaway checkout the `narrow_broad` rule tests drive, shared by three suites.

`test_deploy_tags_narrow.py` (the rules), `test_deploy_tags_narrow_cmd.py` (the command
wrapper) and `test_shared_role_reach.py` (the shared-role reach question, which builds its
own tree from `Tree` rather than from `build_tree`) all need a repo with the shape the rules read -- a `containers_list`, two
`group_vars` keys with different consumer spreads, a shared macro with one importer, and a
play-level file -- with every `GIT_*` variable stripped from the git calls that build it.
Under a prek hook `GIT_DIR`/`GIT_INDEX_FILE` are exported and `cwd` loses, so an unscrubbed
fixture writes the REAL repository's config. Same split as `_release_fixtures.py` beside
`test_probe_releases_stale.py`, and imported by bare name for the same reason: pytest puts
a test's own directory on `sys.path`.
"""

import os
import subprocess
from pathlib import Path

import narrow_broad

# The host_vars this fixture declares. Six services, so a key two roles read stays under the
# coverage ceiling while a key four roles read trips it.
DECLARED = {"sonarr", "radarr", "bazarr", "lidarr", "prowlarr", "jellyfin"}


def _repo_git(repo, *args: str) -> str:
    """One git command in `repo`, with every inherited GIT_* variable removed."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@example.invalid"
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


class Tree:
    """A throwaway checkout the narrowing rules can be driven against."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        _repo_git(root, "init", "-q", "-b", "master")

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def remove(self, rel: str) -> None:
        (self.root / rel).unlink()

    def commit(self, message: str) -> str:
        _repo_git(self.root, "add", "-A")
        _repo_git(self.root, "commit", "-q", "-m", message, "--no-gpg-sign")
        return _repo_git(self.root, "rev-parse", "HEAD")

    def narrow(self, old: str, new: str) -> set[str]:
        """`narrow_broad.narrow` against this tree, with the real repo's tree kept out."""
        return narrow_broad.narrow(
            old, new, cwd=self.root, declared=DECLARED, callers={}
        )


HOST_VARS = """\
containers_list:
  - name: sonarr
    platform: k8s
    image: sonarr:1
  - name: radarr
    platform: k8s
  - name: bazarr
    platform: k8s
  - name: lidarr
    platform: k8s
  - name: prowlarr
    platform: k8s
  - name: jellyfin
    platform: k8s
"""

GROUP_VARS = """\
# The LAN, read by two roles.
lan_subnet: 10.0.0.0/24
fleet_key: everywhere
play_key: read-by-the-play
unused_key: nobody-reads-this
"""


def build_tree(tmp_path) -> Tree:
    """A checkout with one consumer of each kind, committed as the base commit.

    The body of every `tree` fixture: each test module wraps it in its own three-line
    fixture, so no module imports a fixture by name (ruff reads a fixture parameter that
    shadows an imported name as F811).
    """
    t = Tree(tmp_path / "repo")
    t.write("ansible/inventory/host_vars/daniel-box.yml", HOST_VARS)
    t.write("ansible/inventory/group_vars/all.yml", GROUP_VARS)
    t.write("ansible/inventory/hosts.ini", "[all]\ndaniel-box\n")
    t.write("ansible/deploy.yml", "- hosts: all\n  vars:\n    p: '{{ play_key }}'\n")
    # sonarr and radarr read lan_subnet; four roles read fleet_key; sonarr alone imports the
    # macro. jellyfin reads neither, so a rule that fired on every role would show here.
    t.write(
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2",
        "{% from 'container-resources.yml.j2' import resources %}\n"
        "net: {{ lan_subnet }}\nf: {{ fleet_key }}\n",
    )
    t.write(
        "ansible/roles/k8s/radarr/templates/deployment.yaml.j2",
        "net: {{ lan_subnet }}\nf: {{ fleet_key }}\n",
    )
    for role in ("bazarr", "lidarr"):
        t.write(
            f"ansible/roles/k8s/{role}/templates/deployment.yaml.j2",
            "f: {{ fleet_key }}\n",
        )
    t.write("ansible/roles/k8s/jellyfin/templates/deployment.yaml.j2", "a: b\n")
    t.write("ansible/templates/container-resources.yml.j2", "{% macro resources() %}\n")
    t.write("ansible/templates/orphan.yml.j2", "{% macro orphan() %}\n")
    t.commit("base")
    return t


def _refs(tree: Tree, message: str = "change") -> tuple[str, str]:
    old = _repo_git(tree.root, "rev-parse", "HEAD")
    return old, tree.commit(message)
