"""The throwaway setup role the `narrow_setup` suites drive, shared by the two modules.

`test_narrow_setup.py` (the rules `narrow_setup.role_tags` states in its own docstring) and
`test_narrow_setup_edges.py` (the scan edges underneath them) both need a role with the shape
every real setup role has: an untagged `tasks/main.yml` importing one task file per tag, a
template each task renders, and a `defaults/` structure naming a template no `src:` mentions.

`Tree` itself comes from `_narrow_fixtures.py` beside this, which strips every inherited
`GIT_*` variable — under a prek hook an unscrubbed fixture writes the REAL repository's
config. Imported by bare name for the reason that module gives: pytest puts a test's own
directory on `sys.path`.
"""

import narrow_setup

from _narrow_fixtures import Tree

ROLE = "ansible/roles/setup/demo"

# One task file per tag, the shape every setup role has: `main.yml` imports them and carries
# no tags of its own, so it is the untagged file the refusals key on.
MAIN = """\
---
- name: The first topic
  ansible.builtin.import_tasks: alpha.yml
- name: The second topic
  ansible.builtin.import_tasks: beta.yml
"""

ALPHA = """\
---
- name: Render the alpha config
  ansible.builtin.template:
    src: alpha.conf.j2
    dest: /etc/alpha.conf
  tags: [alpha]
- name: Restart alpha
  ansible.builtin.systemd:
    name: alpha
    state: restarted
  tags: [alpha]
"""

BETA = """\
---
- name: Render the beta config
  ansible.builtin.template:
    src: beta.conf.j2
    dest: /etc/beta.conf
  tags: [beta]
- name: Release the beta cron scripts
  ansible.builtin.import_tasks: "{{ role_path }}/../common/tasks/release_bin.yml"
  vars:
    release_bin_templates: "{{ demo_release_groups | map(attribute='templates') | flatten }}"
  tags: [beta]
"""

DEFAULTS = """\
---
demo_alpha_mode: fast
demo_beta_mode: slow
demo_orphan_key: nobody-reads-this
# A host script's template named in a data structure rather than in a task's `src:` — the
# shape `setup/k3s` uses for its cron scripts, through `k3s_render_stamp_groups`.
demo_release_groups:
  - name: demo-beta
    templates:
      - beta-cron.sh.j2
"""


# The playbook the remediation prints for `demo`. The derivation refuses a role no play in it
# lists, since none of the role's own tags reach a host through it.
PLAYBOOK = "ansible/demo.yml"
PLAYBOOK_TEXT = """\
---
- name: Apply the demo role
  hosts: localhost
  roles:
    - { role: demo, tags: ["demo"] }
"""


def build(tmp_path) -> Tree:
    """A checkout holding one setup role with two tagged task files and one untagged one."""
    tree = Tree(tmp_path / "repo")
    tree.write(PLAYBOOK, PLAYBOOK_TEXT)
    tree.write(f"{ROLE}/tasks/main.yml", MAIN)
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA)
    tree.write(f"{ROLE}/tasks/beta.yml", BETA)
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "mode = {{ demo_alpha_mode }}\n")
    tree.write(f"{ROLE}/templates/beta.conf.j2", "mode = {{ demo_beta_mode }}\n")
    tree.write(f"{ROLE}/templates/beta-cron.sh.j2", "#!/bin/sh\necho beta\n")
    tree.write(f"{ROLE}/defaults/main.yml", DEFAULTS)
    tree.write(
        f"{ROLE}/handlers/main.yml", "---\n- name: noop\n  ansible.builtin.debug: {}\n"
    )
    tree.commit("base")
    return tree


def narrow(tree: Tree, old: str, new: str) -> frozenset[str]:
    """`narrow_setup.role_tags` for the demo role, against this tree and its playbook."""
    return narrow_setup.role_tags("demo", "demo", old, new, str(tree.root), PLAYBOOK)
