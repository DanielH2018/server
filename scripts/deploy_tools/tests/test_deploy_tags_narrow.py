"""What `deploy_tags.py narrow` maps a deploy-plane range to, and what it refuses.

Every rule in `narrow_broad.py` is a PAIR here: one range it narrows to a tag list (or to
nothing, on purpose) and one it refuses with `DEPLOY_BROAD`. A narrowing that fired on
everything and one that fired on nothing read identically from the passing side alone, and
this module is the only place that tells them apart.

The fixture builds a throwaway git repo with the shape the rules read — a `containers_list`,
two `group_vars` keys with different consumer spreads, a shared macro with one importer, and
a play-level file — and strips every `GIT_*` variable from the git calls that build it. Under
a prek hook `GIT_DIR`/`GIT_INDEX_FILE` are exported and `cwd` loses, so an unscrubbed fixture
writes the REAL repository's config.

`test_the_real_tree_still_has_macro_importers` is the non-vacuity half: the importer scan
finds its subject by pattern, so a changed Jinja spelling would make it return an empty set
that reads as "this macro reaches nothing" rather than as a broken scan.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_tags_narrow.py
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import narrow_broad
from deploy_tools.exit_codes import DEPLOY_BROAD, DEPLOY_OK
from lib.repo_paths import REPO

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


@pytest.fixture
def tree(tmp_path) -> Tree:
    """A checkout with one consumer of each kind, committed as the base commit."""
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


# ── containers_list: an entry maps to its own tag ───────────────────────────────────────


def test_a_changed_containers_list_entry_narrows_to_that_service(tree: Tree):
    tree.write(
        "ansible/inventory/host_vars/daniel-box.yml",
        HOST_VARS.replace("image: sonarr:1", "image: sonarr:2"),
    )
    assert tree.narrow(*_refs(tree)) == {"sonarr"}


def test_an_added_containers_list_entry_narrows_to_its_tag(tree: Tree):
    tree.write(
        "ansible/inventory/host_vars/daniel-box.yml",
        HOST_VARS + "  - name: jellyfin-new\n    platform: k8s\n",
    )
    assert tree.narrow(*_refs(tree)) == {"jellyfin-new"}


def test_a_removed_containers_list_entry_is_flagged(tree: Tree):
    """A removal has no tag that undoes it — only the whole play reconciles the host."""
    tree.write(
        "ansible/inventory/host_vars/daniel-box.yml",
        HOST_VARS.replace("  - name: bazarr\n    platform: k8s\n", ""),
    )
    with pytest.raises(narrow_broad.CannotNarrow, match="removed"):
        tree.narrow(*_refs(tree))


# ── a plain inventory key maps to the roles that read it ────────────────────────────────


def test_a_group_vars_key_two_roles_read_narrows_to_both(tree: Tree):
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("10.0.0.0/24", "10.1.0.0/24"),
    )
    assert tree.narrow(*_refs(tree)) == {"sonarr", "radarr"}


def test_a_group_vars_key_the_play_reads_is_flagged(tree: Tree):
    """`ansible/deploy.yml` reads it, so no tag scopes the change."""
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("read-by-the-play", "read-by-the-play-still"),
    )
    with pytest.raises(narrow_broad.CannotNarrow, match="ansible/deploy.yml"):
        tree.narrow(*_refs(tree))


def test_a_group_vars_key_nothing_reads_narrows_to_nothing(tree: Tree):
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("nobody-reads-this", "still-nobody"),
    )
    assert tree.narrow(*_refs(tree)) == set()


def test_a_key_most_of_the_fleet_reads_is_flagged(tree: Tree):
    """Four of six declared services read it: a tag list that wide is not a narrowing."""
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("everywhere", "everywhere-else"),
    )
    with pytest.raises(narrow_broad.CannotNarrow, match="most of the fleet"):
        tree.narrow(*_refs(tree))


def test_a_comment_only_inventory_change_narrows_to_nothing(tree: Tree):
    """The rejecting half of every key rule: no content moved, so nothing is stale.

    There is no comment-only rule to reach it — the keys are compared as parsed VALUES, and
    a comment is not one. `comment_only_broad_changes` answers the same question for the
    setup plane by comparing lines, which is what a plane with no key structure needs.
    """
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("# The LAN, read by two roles.", "# The LAN. Two roles.\n"),
    )
    assert tree.narrow(*_refs(tree)) == set()


# ── shared macros map to their importers ────────────────────────────────────────────────


def test_a_macro_change_narrows_to_its_importers(tree: Tree):
    tree.write(
        "ansible/templates/container-resources.yml.j2",
        "{% macro resources(cpu) %}\n",
    )
    assert tree.narrow(*_refs(tree)) == {"sonarr"}


def test_a_macro_nothing_imports_narrows_to_nothing(tree: Tree):
    tree.write("ansible/templates/orphan.yml.j2", "{% macro orphan(x) %}\n")
    assert tree.narrow(*_refs(tree)) == set()


def test_the_real_tree_still_has_macro_importers():
    """Non-vacuity: the importer scan must still find the macro 82 role templates import.

    It finds its subject by pattern, so a changed Jinja spelling returns an empty set that
    reads as "this macro reaches nothing" — a narrowing to nothing where a fleet-wide refusal
    belongs.
    """
    found = narrow_broad.template_importers(
        "container-resources.yml.j2", "HEAD", REPO, set()
    )
    assert len(found) >= 20, found


# ── the paths no rule can scope ─────────────────────────────────────────────────────────


def test_a_play_level_file_is_flagged(tree: Tree):
    tree.write("ansible/deploy.yml", "- hosts: all\n  vars:\n    p: 'x'\n")
    with pytest.raises(narrow_broad.CannotNarrow, match="ansible/deploy.yml"):
        tree.narrow(*_refs(tree))


def test_hosts_ini_is_flagged(tree: Tree):
    tree.write("ansible/inventory/hosts.ini", "[all]\ndaniel-box\ndaniel-pi\n")
    with pytest.raises(narrow_broad.CannotNarrow, match="hosts.ini"):
        tree.narrow(*_refs(tree))


def test_a_deleted_broad_path_is_flagged(tree: Tree):
    """Nothing can be read at the new ref, so nothing can be derived from it."""
    tree.remove("ansible/templates/orphan.yml.j2")
    with pytest.raises(narrow_broad.CannotNarrow, match="deleted"):
        tree.narrow(*_refs(tree))


# ── the non-broad half is the mapper `changed` already uses ─────────────────────────────


def test_a_non_broad_path_maps_the_way_changed_maps_it(tree: Tree):
    """A k8s role beside the inventory change contributes the tag `changed` would derive."""
    tree.write("ansible/roles/k8s/jellyfin/templates/deployment.yaml.j2", "a: c\n")
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("10.0.0.0/24", "10.2.0.0/24"),
    )
    assert tree.narrow(*_refs(tree)) == {"jellyfin", "sonarr", "radarr"}


def test_a_setup_plane_path_in_the_range_is_flagged(tree: Tree):
    """Mixed planes: the setup half has its own arm, and this one must not claim it."""
    tree.write("ansible/roles/setup/k3s/defaults/main.yml", "k3s_x: 1\n")
    with pytest.raises(narrow_broad.CannotNarrow, match="setup"):
        tree.narrow(*_refs(tree))


# ── the command wrapper ─────────────────────────────────────────────────────────────────


def test_the_command_prints_the_tags_and_exits_zero(tree: Tree, capsys):
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("10.0.0.0/24", "10.3.0.0/24"),
    )
    old, new = _refs(tree)
    rc = narrow_broad.narrow_cmd(old, new, cwd=tree.root, declared=DECLARED, callers={})
    assert rc == DEPLOY_OK
    out = capsys.readouterr()
    assert out.out.strip() == "radarr,sonarr"
    assert "lan_subnet" in out.err


def test_the_command_prints_nothing_and_exits_zero_when_it_narrows_to_nothing(
    tree: Tree, capsys
):
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("nobody-reads-this", "still-nobody"),
    )
    old, new = _refs(tree)
    rc = narrow_broad.narrow_cmd(old, new, cwd=tree.root, declared=DECLARED, callers={})
    assert rc == DEPLOY_OK
    assert capsys.readouterr().out == ""


def test_the_command_exits_three_when_it_cannot_narrow(tree: Tree, capsys):
    tree.write("ansible/inventory/hosts.ini", "[all]\ndaniel-box\ndaniel-pi\n")
    old, new = _refs(tree)
    rc = narrow_broad.narrow_cmd(old, new, cwd=tree.root, declared=DECLARED, callers={})
    assert rc == DEPLOY_BROAD
    assert "hosts.ini" in capsys.readouterr().err
