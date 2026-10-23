"""A setup role's leading `meta: end_role` guard reaches its *Applied by* glance line.

H4 ends `nut_host` on the UPS host with a guard in the role, so the role's At-a-glance block
must name the hosts it ends on. The accept half is a leading guard, whose text the line
carries. The reject half is a role without one, and a guard that is not the first task, both
of which leave the line as it was.

Run: uv run pytest scripts/docs/tests/test_gen_role_glance_end_role.py
"""

from docs.catalog_lib import glance_facts as f

PLAYBOOK = """\
- hosts: all
  roles:
    - { role: widget, tags: [widget] }
"""
GUARD = """\
- name: End the role on the UPS host
  ansible.builtin.meta: end_role
  when: inventory_hostname == ups_host
"""
WRITE = """\
- name: Write the config
  ansible.builtin.copy:
    dest: /etc/widget.conf
    content: x
"""
APPLIED = '- **Applied by:** `initial_setup.yml --tags "widget"`'


def _role(tmp_path, tasks: str):
    (tmp_path / "initial_setup.yml").write_text(PLAYBOOK)
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(tasks)
    return role


def test_a_leading_guard_adds_its_text_to_the_applied_by_line(tmp_path):
    role = _role(tmp_path, GUARD + WRITE)
    assert f.setup_glance_lines(role, playbooks_dir=tmp_path)[0] == (
        f"{APPLIED}; ends on hosts where `inventory_hostname == ups_host` "
        "(leading `meta: end_role`)"
    )


def test_a_list_guard_is_joined_with_and(tmp_path):
    guard = GUARD.replace(
        "when: inventory_hostname == ups_host",
        "when:\n    - inventory_hostname == ups_host\n    - ups_armed",
    )
    role = _role(tmp_path, guard + WRITE)
    assert f.setup_glance_lines(role, playbooks_dir=tmp_path)[0].endswith(
        "where `inventory_hostname == ups_host and ups_armed` (leading `meta: end_role`)"
    )


def test_a_role_without_a_guard_keeps_the_line(tmp_path):
    role = _role(tmp_path, WRITE)
    assert f.setup_glance_lines(role, playbooks_dir=tmp_path)[0] == APPLIED


def test_a_guard_that_is_not_the_first_task_keeps_the_line(tmp_path):
    role = _role(tmp_path, WRITE + GUARD)
    assert f.setup_glance_lines(role, playbooks_dir=tmp_path)[0] == APPLIED
