"""A `secrets.yml` change reaches the readers of the keys it moved (#5409).

The fixture is `_narrow_fixtures.build_tree` plus a SOPS-shaped `secrets.yml`: two keys a k8s
role reads, one only a setup role reads, and one only the manifests role's guard map names.
Each value stands in for ciphertext, which SOPS leaves byte-identical for a key an edit did
not touch.
"""

import json

import pytest

import narrow_setup
import secret_setup_readers
from deploy_tools.narrow_lib import role_reads
from deployer_lib.deploy_setup_roles import SECRET_READERS_REFUSED_EXIT
from lib.exit_codes import DEPLOY_BROAD, FAILED
from lib.narrow_git import CannotNarrow

from _narrow_fixtures import Tree, _refs, build_tree

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


# ── the setup plane's half (#5436) ────────────────────────────────────────────────────────────
SETUP_PLAY = """\
- name: Apply claude_code
  hosts: localhost
  roles:
    - { role: claude_code, tags: ["claude_code"] }
"""


def _setup_readers(tree: Tree, capsys) -> tuple[int, dict]:
    """`secret_setup_readers.py` over the tree's last commit, as the tick runs it."""
    old, new = _refs(tree)
    rc = secret_setup_readers.main([old, new, "--repo", str(tree.root)])
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out) if out else {}


def test_a_new_key_a_setup_role_reads_names_that_roles_file(tree: Tree, capsys):
    """PR #5405's key: the deploy plane maps it to nothing, and the tick needs the role."""
    tree.write(SECRETS, _with(BASE_SECRETS, "new_setup_token", "eee"))
    assert _setup_readers(tree, capsys) == (
        0,
        {
            "paths": ["ansible/roles/setup/claude_code/templates/env.j2"],
            "unread": {},
            "refused": "",
        },
    )


def test_a_key_only_a_k8s_role_reads_names_no_setup_file(tree: Tree, capsys):
    tree.write(SECRETS, _with(BASE_SECRETS, "jellyfin_api_key", "zzz"))
    assert _setup_readers(tree, capsys) == (
        0,
        {"paths": [], "unread": {}, "refused": ""},
    )


def test_a_key_the_play_reads_refuses_the_setup_answer(tree: Tree, capsys):
    """A play-level reader may feed a setup role under another name, so this cannot say."""
    tree.write("ansible/deploy.yml", PLAY + "    k: '{{ setup_only_token }}'\n")
    tree.commit("the play reads a secret")
    tree.write(SECRETS, _with(BASE_SECRETS, "setup_only_token", "xxx"))
    assert _setup_readers(tree, capsys) == (DEPLOY_BROAD, {})


# ── the deploy plane's half (#5577) ──────────────────────────


def _deploy_tags(tree: Tree, capsys) -> tuple[int, str]:
    """`secret_setup_readers.py --deploy-tags` over the tree's last commit, as the tick runs it."""
    old, new = _refs(tree)
    rc = secret_setup_readers.main(
        ["--deploy-tags", old, new, "--repo", str(tree.root)]
    )
    return rc, capsys.readouterr().out.strip()


def test_deploy_tags_names_the_k8s_reader_of_a_rotated_key(tree: Tree, capsys):
    tree.write(SECRETS, _with(BASE_SECRETS, "jellyfin_api_key", "zzz"))
    assert _deploy_tags(tree, capsys) == (0, "jellyfin")


def test_deploy_tags_is_empty_for_a_key_only_a_setup_role_reads(tree: Tree, capsys):
    tree.write(SECRETS, _with(BASE_SECRETS, "new_setup_token", "eee"))
    assert _deploy_tags(tree, capsys) == (0, "")


def test_deploy_tags_refuses_a_key_the_play_reads_naming_nothing(tree: Tree, capsys):
    tree.write("ansible/deploy.yml", PLAY + "    k: '{{ bazarr_api_key }}'\n")
    tree.commit("the play reads a secret")
    tree.write(SECRETS, _with(BASE_SECRETS, "bazarr_api_key", "xxx"))
    assert _deploy_tags(tree, capsys) == (DEPLOY_BROAD, "")


def test_deploy_tags_refuses_most_of_the_fleet_naming_it(tree: Tree, capsys):
    """The coverage ceiling: four of the six declared services read the key."""
    for role in ("sonarr", "radarr", "lidarr"):
        tree.write(
            f"ansible/roles/k8s/{role}/templates/token.yaml.j2",
            "t: {{ bazarr_api_key }}\n",
        )
    tree.commit("three more readers")
    tree.write(SECRETS, _with(BASE_SECRETS, "bazarr_api_key", "xxx"))
    assert _deploy_tags(tree, capsys) == (DEPLOY_BROAD, "bazarr,lidarr,radarr,sonarr")


SHIPPED_DEFAULTS = "ansible/roles/setup/k3s/defaults/main.yml"


def _ship_k3s_defaults(tree: Tree, deploy_ui_reads: str) -> None:
    """k3s's defaults build one key from a secret, and deploy_ui reads the file.

    The shape is the real tree's row in `SETUP_FILES_SHIPPED_BY_OTHER_ROLES` (#4303), which
    this reads unpatched: a change to that row changes what these tests exercise.
    """
    tree.write(
        SHIPPED_DEFAULTS,
        'setup_built_token: "{{ setup_only_token }}"\nsetup_plain: 1\n',
    )
    tree.write(
        "ansible/roles/setup/deploy_ui/tasks/main.yml",
        f"- name: Read\n  debug: {{msg: '{{{{ {deploy_ui_reads} }}}}'}}\n",
    )
    tree.commit("a shipped defaults file")
    tree.write(SECRETS, _with(BASE_SECRETS, "setup_only_token", "xxx"))


def test_a_shipper_reading_none_of_the_secrets_keys_is_unread(tree: Tree, capsys):
    """deploy_ui reads k3s's defaults for one plain key, so a secret skips it."""
    _ship_k3s_defaults(tree, "setup_plain")
    rc, out = _setup_readers(tree, capsys)
    assert rc == 0 and out["unread"] == {SHIPPED_DEFAULTS: ["deploy_ui"]}


def test_a_shipper_reading_a_key_built_from_the_secret_is_a_reader(tree: Tree, capsys):
    """The rejecting half: the key built from the secret reaches deploy_ui's task."""
    _ship_k3s_defaults(tree, "setup_built_token")
    rc, out = _setup_readers(tree, capsys)
    assert rc == 0 and SHIPPED_DEFAULTS in out["paths"] and out["unread"] == {}


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


def test_a_shipper_reading_none_of_the_secrets_keys_keeps_its_narrow_answer(
    tree: Tree,
):
    """`role_tags` takes the routing's rule: deploy_ui's prose edit still needs nothing."""
    _ship_k3s_defaults(tree, "setup_plain")
    tree.write(
        "ansible/initial_setup.yml", SETUP_PLAY.replace("claude_code", "deploy_ui")
    )
    tree.write("ansible/roles/setup/deploy_ui/CLAUDE.md", "# deploy_ui\n")
    old, new = _refs(tree)
    with pytest.raises(narrow_setup.NothingToApply):
        narrow_setup.role_tags(
            "deploy_ui",
            "deploy_ui",
            old,
            new,
            str(tree.root),
            "ansible/initial_setup.yml",
        )


def test_the_deployer_reads_the_scripts_refusal_code_and_no_crash_shares_it():
    """The tick merges on a refusal and parks on a crash, which Python exits 1 with."""
    assert SECRET_READERS_REFUSED_EXIT == DEPLOY_BROAD != FAILED


# ── the `role_reads` rules, through the script the tick runs ─────────────────────────────────
EXTRA_VARS = "ansible/inventory/group_vars/extra.yml"
SETUP_READER = "ansible/roles/setup/claude_code/templates/env.j2"


def _refusal(tree: Tree, capsys, *argv: str) -> tuple[int, str]:
    """`secret_setup_readers.py` over the tree's last commit: its exit and its stderr."""
    old, new = _refs(tree)
    rc = secret_setup_readers.main([*argv, old, new, "--repo", str(tree.root)])
    return rc, capsys.readouterr().err


@pytest.mark.parametrize(
    "value",
    ['consumer: "{{ setup_only_token }}"\n', "plain: 1  # setup_only_token\n"],
    ids=["templated-value", "trailing-comment"],
)
def test_a_secret_another_inventory_value_reads_is_flagged(
    tree: Tree, capsys, value: str
):
    """The value's own parsed form does not move, so its readers stay out of the key walk."""
    tree.write(EXTRA_VARS, value)
    tree.commit("an inventory value names the secret")
    tree.write(SECRETS, _with(BASE_SECRETS, "setup_only_token", "xxx"))
    rc, err = _refusal(tree, capsys)
    assert rc == DEPLOY_BROAD
    assert "read by another value" in err


@pytest.mark.parametrize(
    "path, text",
    [
        (EXTRA_VARS, "setup_only_token: plain\n"),
        (EXTRA_VARS, "# setup_only_token is documented here\nplain: 1\n"),
        ("ansible/inventory/_example.yml", 'consumer: "{{ setup_only_token }}"\n'),
    ],
    ids=["its-own-definition", "whole-line-comment", "file-no-host-loads"],
)
def test_an_inventory_mention_that_reads_nothing_is_clean(
    tree: Tree, capsys, path: str, text: str
):
    tree.write(path, text)
    tree.commit("the inventory mentions the secret")
    tree.write(SECRETS, _with(BASE_SECRETS, "setup_only_token", "xxx"))
    rc, found = _setup_readers(tree, capsys)
    assert rc == 0
    assert found["paths"] == [SETUP_READER]


def test_a_secret_a_shared_template_names_is_flagged(tree: Tree, capsys):
    """A setup role may import that template, and the key walk does not follow it."""
    tree.write("ansible/templates/shared.yml.j2", "k: {{ setup_only_token }}\n")
    tree.commit("a shared template names the secret")
    tree.write(SECRETS, _with(BASE_SECRETS, "setup_only_token", "xxx"))
    rc, err = _refusal(tree, capsys)
    assert rc == DEPLOY_BROAD
    assert "shared template shared.yml.j2" in err


def test_deploy_tags_skips_a_key_only_the_manifests_guard_map_names(tree: Tree, capsys):
    """The guard map renders no key. Counted, it maps to `manifests`, which no tag applies."""
    tree.write(SECRETS, _with(BASE_SECRETS, "guard_only_key", "yyy"))
    assert _deploy_tags(tree, capsys) == (0, "")


def test_deploy_tags_adds_the_role_rendering_a_key_through_lookup(tree: Tree, capsys):
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
    assert _deploy_tags(tree, capsys) == (0, "jellyfin,radarr,sonarr")


def test_deploy_tags_follows_a_macro_another_macro_imports(tree: Tree, capsys):
    """The two macros import each other, so the walk also has to stop."""
    tree.write(
        "ansible/templates/inner.yml.j2",
        "{% from 'outer.yml.j2' import o %}k: {{ jellyfin_api_key }}\n",
    )
    tree.write("ansible/templates/outer.yml.j2", "{% from 'inner.yml.j2' import i %}\n")
    tree.write(
        "ansible/roles/k8s/lidarr/templates/d.yaml.j2",
        "{% from 'outer.yml.j2' import o %}\n",
    )
    tree.commit("a macro chain")
    tree.write(SECRETS, _with(BASE_SECRETS, "jellyfin_api_key", "zzz"))
    assert _deploy_tags(tree, capsys) == (0, "jellyfin,lidarr")


def _include(tree: Tree, caller: str, helper: str) -> None:
    tree.write(
        f"ansible/roles/k8s/{caller}/tasks/{helper}.yml",
        f"- ansible.builtin.include_role:\n    name: k8s/{helper}\n",
    )


def test_deploy_tags_maps_a_shared_role_to_its_callers_and_theirs(tree: Tree, capsys):
    """`helper` and `middle` call each other, so the walk also has to stop."""
    tree.write(
        "ansible/roles/k8s/helper/templates/s.yaml.j2", "k: {{ jellyfin_api_key }}\n"
    )
    _include(tree, "middle", "helper")
    _include(tree, "helper", "middle")
    _include(tree, "lidarr", "middle")
    tree.commit("a shared role reads the key")
    tree.write(SECRETS, _with(BASE_SECRETS, "jellyfin_api_key", "zzz"))
    assert _deploy_tags(tree, capsys) == (0, "jellyfin,lidarr")


def test_deploy_tags_refuses_a_shared_role_nothing_calls(tree: Tree, capsys):
    tree.write(
        "ansible/roles/k8s/orphan/templates/s.yaml.j2", "k: {{ jellyfin_api_key }}\n"
    )
    tree.commit("a role with no entry and no caller reads the key")
    tree.write(SECRETS, _with(BASE_SECRETS, "jellyfin_api_key", "zzz"))
    rc, err = _refusal(tree, capsys, "--deploy-tags")
    assert rc == DEPLOY_BROAD
    assert "orphan has no containers_list entry and no caller" in err


def test_deploy_tags_refuses_a_host_vars_file_that_does_not_parse(tree: Tree, capsys):
    tree.write("ansible/inventory/host_vars/daniel-box.yml", "containers_list: [\n")
    tree.write(SECRETS, _with(BASE_SECRETS, "jellyfin_api_key", "zzz"))
    rc, err = _refusal(tree, capsys, "--deploy-tags")
    assert rc == DEPLOY_BROAD
    assert "does not parse" in err


def _ctx(tree: Tree, ref: str = "HEAD") -> role_reads.Context:
    return role_reads.Context(tree.root, ref, set(), {}, lambda _m: None)


def test_a_macro_scan_drops_a_filter_plugin_naming_the_macro(tree: Tree):
    """`toposort.py` names `ingressroute.yml.j2`, and can never render it."""
    hits = ["ansible/filter_plugins/toposort.py"]
    assert role_reads.sort_hits(hits, "the macro m", _ctx(tree)).roles == set()


def test_a_variable_a_filter_plugin_names_is_flagged(tree: Tree):
    hits = ["ansible/filter_plugins/toposort.py"]
    with pytest.raises(CannotNarrow, match="which every deploy runs"):
        role_reads.sort_hits(hits, "the variable v", _ctx(tree), "v")


def test_a_macro_scan_refuses_an_inventory_hit_it_cannot_judge(tree: Tree):
    hits = ["ansible/inventory/group_vars/all.yml"]
    with pytest.raises(CannotNarrow, match="with no key"):
        role_reads.sort_hits(hits, "the macro m", _ctx(tree))


def test_a_grep_git_cannot_run_is_flagged(tree: Tree):
    with pytest.raises(CannotNarrow, match="failed"):
        role_reads.grep(_ctx(tree, "0" * 40), "k", word=True)
