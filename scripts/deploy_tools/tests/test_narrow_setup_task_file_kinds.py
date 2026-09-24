"""Which files under a role's `tasks/` the index reads, and which entry point it starts from.

Ansible's `Role._load_role_yaml` loads `main` with `.yml`, `.yaml`, `.json` or no extension at
all, and `import_tasks` can name any of those shapes. Reading only `.yml` and `.yaml` skipped
the other two (#2434): a task file the index skips contributes no tags, so a template or a
vars key it also reads narrowed to fewer tags than the change needed. A `tasks/main` under
one of the skipped extensions was worse still — `reachable` came back empty and every file of
the role refused.

No setup role carries such a file, so the demo tree is the only subject these have; the
`_LOADED_EXTENSIONS` census at the end is what ties the predicate back to Ansible's own list.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup_task_file_kinds.py
"""

import importlib
import json

import pytest

import narrow_setup
import narrow_setup_index

from _setup_role_fixtures import MAIN, ROLE, Tree, build, narrow

# A second renderer of `alpha.conf.j2` under its own tag. The template is already rendered by
# `alpha.yml`, so a skipped second reader shows up as a missing tag rather than a refusal.
GAMMA_TASKS = [
    {
        "name": "Render the alpha config for gamma too",
        "ansible.builtin.template": {
            "src": "alpha.conf.j2",
            "dest": "/etc/alpha-gamma.conf",
        },
        "tags": ["gamma"],
    }
]

GAMMA_YAML = """\
---
- name: Render the alpha config for gamma too
  ansible.builtin.template:
    src: alpha.conf.j2
    dest: /etc/alpha-gamma.conf
  tags: [gamma]
"""


@pytest.fixture
def tree(tmp_path) -> Tree:
    return build(tmp_path)


def _with_gamma(tree: Tree, rel: str, text: str) -> str:
    """Commit a second renderer of `alpha.conf.j2` at `rel`, imported from `main.yml`."""
    tree.write(f"{ROLE}/tasks/{rel}", text)
    tree.write(
        f"{ROLE}/tasks/main.yml",
        MAIN + f"- name: The third topic\n  ansible.builtin.import_tasks: {rel}\n",
    )
    return tree.commit(f"a task file at tasks/{rel}")


def _edit_the_template(tree: Tree) -> str:
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "mode = {{ demo_alpha_mode }} # v2\n")
    return tree.commit("edit the alpha template")


# ── the four extensions Ansible loads, each contributing its tag ────────────────────────


def test_a_yaml_task_file_contributes_its_tags(tree):
    """The polarity that always worked, so the three below are read against it."""
    old = _with_gamma(tree, "gamma.yml", GAMMA_YAML)
    assert narrow(tree, old, _edit_the_template(tree)) == frozenset({"alpha", "gamma"})


def test_a_json_task_file_contributes_its_tags(tree):
    """`Role._load_role_yaml` loads `.json`, and `import_tasks` can name one."""
    old = _with_gamma(tree, "gamma.json", json.dumps(GAMMA_TASKS, indent=2) + "\n")
    assert narrow(tree, old, _edit_the_template(tree)) == frozenset({"alpha", "gamma"})


def test_an_extensionless_task_file_contributes_its_tags(tree):
    """The fourth shape: a bare `tasks/gamma` with no extension at all."""
    old = _with_gamma(tree, "gamma", GAMMA_YAML)
    assert narrow(tree, old, _edit_the_template(tree)) == frozenset({"alpha", "gamma"})


def test_a_markdown_file_under_tasks_is_not_read_as_a_task_file(tree):
    """The rejecting half: prose beside the task files must not refuse the whole role.

    `.md` is not one of the extensions Ansible loads, so the index skips it rather than
    handing it to `file_tags`, which would refuse on "not a list of tasks".
    """
    tree.write(f"{ROLE}/tasks/NOTES.md", "# Why alpha runs first\n")
    old = tree.commit("prose beside the task files")
    assert narrow(tree, old, _edit_the_template(tree)) == frozenset({"alpha"})


# ── the entry point, resolved by Ansible's own precedence ───────────────────────────────


def test_a_role_whose_main_is_json_resolves_its_entry_point(tree):
    """With `tasks/main.yml` gone, `tasks/main.json` is the file Ansible would run."""
    main = [
        {"name": "The first topic", "ansible.builtin.import_tasks": "alpha.yml"},
        {"name": "The second topic", "ansible.builtin.import_tasks": "beta.yml"},
    ]
    tree.write(f"{ROLE}/tasks/main.json", json.dumps(main, indent=2) + "\n")
    tree.remove(f"{ROLE}/tasks/main.yml")
    old = tree.commit("a JSON entry point")
    assert narrow(tree, old, _edit_the_template(tree)) == frozenset({"alpha"})


def test_a_role_with_no_main_file_at_all_is_flagged(tree):
    """`tasks/` holding only files nothing imports reaches no host under the role's entry."""
    tree.remove(f"{ROLE}/tasks/main.yml")
    old = tree.commit("no entry point")
    with pytest.raises(narrow_setup.CannotNarrow, match="holds no main file"):
        narrow(tree, old, _edit_the_template(tree))


def test_the_yaml_main_wins_over_a_json_one(tree):
    """Ansible takes the first extension that exists and the rest are dead files.

    `main.json` here imports nothing, so reading it as the entry point would leave `alpha.yml`
    unreachable and refuse.
    """
    tree.write(f"{ROLE}/tasks/main.json", "[]\n")
    old = tree.commit("a dead JSON main beside the YAML one")
    assert narrow(tree, old, _edit_the_template(tree)) == frozenset({"alpha"})


# ── the predicate against Ansible's own list ────────────────────────────────────────────


def test_the_loaded_extensions_match_ansibles_own_default():
    """The predicate's list is Ansible's, read off the installed `ansible-core`.

    `Role._load_role_yaml` hard-codes its own copy of `YAML_FILENAME_EXTENSIONS` and appends
    `''` for an unnamed `main`, and `_main_task_file` walks that order. Comparing against the
    constant means an ansible-core release that adds an extension fails this rather than
    silently skipping a task file the tree has started to carry.

    Reached through `import_module` rather than `from ansible import constants`: the
    script-coverage scanner matches a by-name import against first-party module stems, and
    `infra_map`'s facade-only member of that name would collect an import credit it has not
    got (`test_a_facade_only_member_gets_no_by_name_import_credit`).
    """
    ansible_constants = importlib.import_module("ansible.constants")

    assert narrow_setup_index._LOADED_EXTENSIONS == (
        *ansible_constants.YAML_FILENAME_EXTENSIONS,
        "",
    )


@pytest.mark.parametrize(
    ("rel", "loaded"),
    [
        ("tasks/main.yml", True),
        ("tasks/main.json", True),
        ("tasks/main", True),
        ("tasks/NOTES.md", False),
        ("tasks/main.yml~", False),
        ("tasks/.main.yml", False),
    ],
)
def test_the_predicate_accepts_what_ansible_loads(rel, loaded):
    assert narrow_setup_index._is_role_yaml(rel) is loaded
