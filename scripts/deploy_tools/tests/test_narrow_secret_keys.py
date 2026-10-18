"""A `secrets.yml` change narrows to the readers of the keys it moved (#5409).

The fixture is `_narrow_fixtures.build_tree` plus a SOPS-shaped `secrets.yml`: two keys a k8s
role reads, one only a setup role reads, and one only the manifests role's guard map names.
Each value stands in for ciphertext, which SOPS leaves byte-identical for a key an edit did
not touch.
"""

import pytest

import narrow_setup
import secret_setup_readers
from deploy_tools.narrow_lib import broad as narrow_broad

from _narrow_fixtures import GROUP_VARS, Tree, _refs, build_tree

SECRETS = "ansible/vars/secrets.yml"
BASE_SECRETS = """\
jellyfin_api_key: ENC[AES256_GCM,data:aaa]
bazarr_api_key: ENC[AES256_GCM,data:bbb]
setup_only_token: ENC[AES256_GCM,data:ccc]
guard_only_key: ENC[AES256_GCM,data:ddd]
sops:
    mac: mac1
    lastmodified: "2026-10-01T00:00:00Z"
"""

PLAY = "- hosts: all\n  vars:\n    s: \"{{ lookup('community.sops.sops_decrypt', 'x') }}\"\n"


def _with(secrets: str, key: str, value: str) -> str:
    """`secrets` with `key` set to `value` and the SOPS MAC moved, as every edit moves it."""
    lines = [ln for ln in secrets.splitlines() if not ln.startswith(f"{key}:")]
    at = lines.index("sops:")
    lines.insert(at, f"{key}: ENC[AES256_GCM,data:{value}]")
    return "\n".join(lines).replace("mac1", "mac2") + "\n"


@pytest.fixture
def tree(tmp_path) -> Tree:
    t = build_tree(tmp_path)
    t.write(SECRETS, BASE_SECRETS)
    # The real play names the SOPS collection, so the metadata key, counted, would refuse.
    t.write("ansible/deploy.yml", PLAY)
    t.write(
        "ansible/roles/k8s/jellyfin/templates/secret.yaml.j2",
        "key: {{ jellyfin_api_key }}\n",
    )
    t.write(
        "ansible/roles/k8s/bazarr/templates/secret.yaml.j2",
        "key: {{ bazarr_api_key }}\n",
    )
    t.write(
        "ansible/roles/setup/claude_code/templates/env.j2",
        "T={{ setup_only_token }}\nN={{ new_setup_token | default('') }}\n",
    )
    t.write(
        "ansible/roles/k8s/manifests/files/standalone_secret_keys.json",
        '["guard_only_key", "new_guard_key"]\n',
    )
    t.commit("secrets")
    return t


def test_a_new_key_only_a_setup_role_reads_beside_an_inventory_change_narrows(
    tree: Tree,
):
    """#5405's shape: a new key for a setup role plus an inventory edit.

    Until #5409 any `secrets.yml` change refused, and the deployer deferred every k8s service
    to `k8s_unapplied`. The key reaches no deploy tag, so only the inventory edit's readers
    remain.
    """
    tree.write(SECRETS, _with(BASE_SECRETS, "new_setup_token", "eee"))
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("10.0.0.0/24", "10.2.0.0/24"),
    )
    assert tree.narrow(*_refs(tree)) == {"sonarr", "radarr"}


def test_a_rotated_key_narrows_to_its_reader_alone(tree: Tree):
    """bazarr's key keeps its ciphertext, so bazarr is not redeployed for jellyfin's."""
    tree.write(SECRETS, _with(BASE_SECRETS, "jellyfin_api_key", "zzz"))
    assert tree.narrow(*_refs(tree)) == {"jellyfin"}


def test_a_key_only_the_manifests_guard_map_names_narrows_to_nothing(tree: Tree):
    """The guard map renders no key. Counted, it maps to `manifests`, which no tag applies."""
    tree.write(SECRETS, _with(BASE_SECRETS, "guard_only_key", "yyy"))
    assert tree.narrow(*_refs(tree)) == set()


def test_a_key_another_role_renders_through_lookup_reaches_both(tree: Tree):
    """uptime-kuma's shape: it renders a push token out of monitor-bridge's `files/`."""
    tree.write(
        "ansible/roles/k8s/sonarr/files/table.py",
        "TOKEN = '{{ jellyfin_api_key }}'\n",
    )
    tree.write(
        "ansible/roles/k8s/radarr/templates/tiles.yaml.j2",
        "{{ lookup('template', playbook_dir + '/roles/k8s/sonarr/files/table.py') }}\n",
    )
    tree.commit("a cross-role lookup")
    tree.write(SECRETS, _with(BASE_SECRETS, "jellyfin_api_key", "zzz"))
    assert tree.narrow(*_refs(tree)) == {"jellyfin", "sonarr", "radarr"}


def test_a_key_the_play_reads_is_flagged(tree: Tree):
    """A play-level reader runs under every tag, so no tag list covers the rotation."""
    tree.write("ansible/deploy.yml", PLAY + "    k: '{{ bazarr_api_key }}'\n")
    tree.commit("the play reads a secret")
    tree.write(SECRETS, _with(BASE_SECRETS, "bazarr_api_key", "xxx"))
    with pytest.raises(narrow_broad.CannotNarrow, match="bazarr_api_key"):
        tree.narrow(*_refs(tree))


# ── the setup plane's half (#5436) ────────────────────────────────────────────────────────────
SETUP_PLAY = """\
- name: Apply claude_code
  hosts: localhost
  roles:
    - { role: claude_code, tags: ["claude_code"] }
"""


def _setup_readers(tree: Tree, capsys) -> tuple[int, str]:
    """`secret_setup_readers.py` over the tree's last commit, as the tick runs it."""
    old, new = _refs(tree)
    rc = secret_setup_readers.main([old, new, "--repo", str(tree.root)])
    return rc, capsys.readouterr().out.strip()


def test_a_new_key_a_setup_role_reads_names_that_roles_file(tree: Tree, capsys):
    """PR #5405's key: the deploy plane maps it to nothing, and the tick needs the role."""
    tree.write(SECRETS, _with(BASE_SECRETS, "new_setup_token", "eee"))
    assert _setup_readers(tree, capsys) == (
        0,
        '["ansible/roles/setup/claude_code/templates/env.j2"]',
    )


def test_a_key_only_a_k8s_role_reads_names_no_setup_file(tree: Tree, capsys):
    tree.write(SECRETS, _with(BASE_SECRETS, "jellyfin_api_key", "zzz"))
    assert _setup_readers(tree, capsys) == (0, "[]")


def test_a_key_the_play_reads_refuses_the_setup_answer(tree: Tree, capsys):
    """A play-level reader may feed a setup role under another name, so this cannot say."""
    tree.write("ansible/deploy.yml", PLAY + "    k: '{{ setup_only_token }}'\n")
    tree.commit("the play reads a secret")
    tree.write(SECRETS, _with(BASE_SECRETS, "setup_only_token", "xxx"))
    assert _setup_readers(tree, capsys) == (secret_setup_readers.FAILED, "")


def _role_tags(tree: Tree, old: str, new: str) -> frozenset[str]:
    return narrow_setup.role_tags(
        "claude_code",
        "claude_code",
        old,
        new,
        str(tree.root),
        "ansible/initial_setup.yml",
    )


@pytest.fixture
def setup_tree(tree: Tree) -> Tree:
    """`tree` plus a playbook applying claude_code, whose own tasks render its template."""
    tree.write("ansible/initial_setup.yml", SETUP_PLAY)
    tree.write(
        "ansible/roles/setup/claude_code/tasks/main.yml",
        "- name: Render\n  template: {src: env.j2, dest: /x}\n  tags: [claude-env]\n",
    )
    tree.write("ansible/roles/setup/claude_code/CLAUDE.md", "# claude_code\n")
    tree.commit("a setup role")
    return tree


def test_a_secret_the_role_reads_widens_it_to_the_whole_role(setup_tree: Tree):
    """A prose edit alone needs no tag; a key the role reads beside it needs the role tag."""
    setup_tree.write(
        "ansible/roles/setup/claude_code/CLAUDE.md", "# claude_code, edited\n"
    )
    setup_tree.write(SECRETS, _with(BASE_SECRETS, "setup_only_token", "xxx"))
    with pytest.raises(narrow_setup.CannotNarrow, match="setup_only_token"):
        _role_tags(setup_tree, *_refs(setup_tree))


def test_a_secret_the_role_does_not_read_leaves_its_answer(setup_tree: Tree):
    """The accepting half: jellyfin's key is not claude_code's, so the prose edit needs nothing."""
    setup_tree.write(
        "ansible/roles/setup/claude_code/CLAUDE.md", "# claude_code, edited\n"
    )
    setup_tree.write(SECRETS, _with(BASE_SECRETS, "jellyfin_api_key", "zzz"))
    with pytest.raises(narrow_setup.NothingToApply):
        _role_tags(setup_tree, *_refs(setup_tree))
