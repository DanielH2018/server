"""Tests for the static values `probe.py applied` reads (`probe_lib/applied_facts.py`).

Each test writes a small role to `tmp_path` and walks it, or runs `run_applied` on it with a
fake host read. Two walk the real `claude_code` role, whose set_facts #6359 and #6378 name.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from diagnostics.probe_lib import applied, applied_tasks
from lib.exit_codes import PROBE_FAILED, PROBE_UNREADABLE

GATED = "- name: Write the gated file\n  ansible.builtin.copy: {content: x, dest: /etc/x/gated}\n"  # fmt: skip
REGISTER = "- name: Read the host\n  ansible.builtin.command: hostname\n  register: x_out\n"  # fmt: skip


def _walk(tmp_path, main, ctx, files=None):
    role = tmp_path / "x"
    for name, text in {"tasks/main.yml": main, "tasks/gated.yml": GATED, **(files or {})}.items():  # fmt: skip
        (role / name).parent.mkdir(parents=True, exist_ok=True)
        (role / name).write_text(text)
    return applied_tasks.expected(role, "daniel-box", ctx)


def _gated(when):
    return f"- name: Import the gated file\n  when: {when}\n  ansible.builtin.import_tasks: gated.yml\n"  # fmt: skip


def test_a_static_set_fact_is_read_as_a_native_list_by_a_later_loop(tmp_path):
    main = (
        "- name: Work out the names\n"
        "  ansible.builtin.set_fact:\n"
        "    x_names: >-\n"
        "      {{ x_agents | selectattr('on') | map(attribute='name') | list }}\n"
        "- name: Each name\n"
        "  ansible.builtin.include_tasks: {file: one.yml}\n"
        "  loop: '{{ x_names }}'\n"
        "  loop_control: {loop_var: x_name}\n" + _gated("x_names | length > 0")
    )
    one = "- name: Mark it\n  ansible.builtin.copy: {content: hi, dest: '/etc/{{ x_name }}'}\n"  # fmt: skip
    ctx = {"x_agents": [{"name": "a", "on": True}, {"name": "b", "on": False}]}
    wants, skips = _walk(tmp_path, main, ctx, {"tasks/one.yml": one})
    assert {w.path for w in wants} == {"/etc/a", "/etc/x/gated"}
    assert skips == []


def test_a_set_fact_value_wins_over_a_default_of_the_same_name(tmp_path):
    main = "- name: Work out the list\n  ansible.builtin.set_fact: {x_list: '{{ [1] }}'}\n" + _gated("x_list | length > 0")  # fmt: skip
    wants, skips = _walk(tmp_path, main, {"x_list": []})
    assert [w.path for w in wants] == ["/etc/x/gated"] and skips == []


# fmt: off
STILL_RUN_TIME = {
    "reads-a-register": REGISTER + "- name: Learn\n  ansible.builtin.set_fact: {x_list: '{{ x_out.stdout_lines }}'}\n",
    "set-twice": "- name: One\n  ansible.builtin.set_fact: {x_list: [1]}\n- name: Two\n  ansible.builtin.set_fact: {x_list: []}\n",
    "looped": "- name: Learn\n  ansible.builtin.set_fact: {x_list: [1]}\n  loop: [1, 2]\n",
}
# fmt: on


@pytest.mark.parametrize("setter", STILL_RUN_TIME.values(), ids=STILL_RUN_TIME)
def test_a_set_fact_the_walk_cannot_settle_stays_run_time(tmp_path, setter):
    _, skips = _walk(tmp_path, setter + _gated("x_list | length > 0"), {})
    assert [(s.unchecked, s.reason) for s in skips] == [(True, "when: reads run-time name(s) x_list")]  # fmt: skip


@pytest.mark.parametrize(("state", "paths"), [("present", ["/etc/x/gated"]), ("absent", [])])  # fmt: skip
def test_an_include_var_keeps_its_native_bool(tmp_path, state, paths):
    """`"{{ x == 'present' }}"` is False, never the truthy string "False"."""
    main = (
        "- name: Each agent\n"
        "  ansible.builtin.include_tasks: {file: agent.yml}\n"
        "  loop: '{{ x_agents }}'\n"
        "  loop_control: {loop_var: x_agent}\n"
        "  vars: {x_on: \"{{ x_agent.state == 'present' }}\"}\n"
    )
    agent = _gated("x_on")
    wants, skips = _walk(tmp_path, main, {"x_agents": [{"state": state}]}, {"tasks/agent.yml": agent})  # fmt: skip
    assert [w.path for w in wants] == paths and not any(s.unchecked for s in skips)


def test_a_vars_lookup_reads_the_vars_or_a_stub_for_a_secret(tmp_path):
    main = (
        "- name: Write the token\n"
        "  ansible.builtin.import_tasks: token.yml\n"
        "  vars:\n"
        "    x_user: \"{{ lookup('vars', 'x_user_var') }}\"\n"
        "    x_token: \"{{ lookup('vars', 'x_token_var', default='') }}\"\n"
    )
    token = "- name: Write it\n  ansible.builtin.template: {src: t.j2, dest: '/etc/{{ x_user }}'}\n"  # fmt: skip
    files = {"tasks/token.yml": token, "templates/t.j2": "{{ x_token }}\n"}
    wants, _ = _walk(tmp_path, main, {"x_user_var": "bob"}, files)
    assert [(w.path, w.sha256, w.unchecked) for w in wants] == [
        ("/etc/bob", None, False)
    ]
    assert "STUB" in wants[0].why_no_content


def _run(tmp_path, template, live):
    """`run_applied` over one template task, with the host reading `live` for its path."""
    main = REGISTER + "- name: Write it\n  ansible.builtin.template: {src: t.j2, dest: /etc/x/t, mode: '0644'}\n"  # fmt: skip
    ctx = {"x_port": 8080}
    _walk(tmp_path, main, ctx, {"templates/t.j2": template})

    def read(plan, host):
        return {"user": "u", "stat": {p: {"status": "ok", **live} for p in plan["stat"]}}  # fmt: skip

    ns = SimpleNamespace(role="x", host="daniel-box", dry_run=False)
    return applied.run_applied(ns, read=read, setup=tmp_path, render_ctx=lambda *_: ctx)


# fmt: off
TEMPLATE_CASES = {
    "run-time-name": ("port={{ x_out.stdout }}\n", {"mode": "0644"}, PROBE_UNREADABLE, "UNCHECKED /etc/x/t  0644 None:None, content not compared: it reads run-time name(s) x_out"),
    "run-time-name-mode-differs": ("port={{ x_out.stdout }}\n", {"mode": "0600"}, PROBE_FAILED, "DRIFT    /etc/x/t  mode 0600 (want 0644)"),
    "inventory-name": ("port={{ x_port }}\n", {"mode": "0644", "sha256": "0" * 64}, PROBE_FAILED, "DRIFT    /etc/x/t  content differs from the render"),
}
# fmt: on


@pytest.mark.parametrize(("template", "live", "code", "line"), TEMPLATE_CASES.values(), ids=TEMPLATE_CASES)  # fmt: skip
def test_a_template_reading_a_run_time_name_is_unchecked_never_drift(tmp_path, capsys, template, live, code, line):  # fmt: skip
    """#6378: content only the run knows is UNCHECKED; mode and owner are still compared."""
    assert _run(tmp_path, template, live) == code
    assert line in capsys.readouterr().out


def test_the_real_fanout_alert_rule_renders_from_the_evaluated_agent_list():
    """#6378's false DRIFT: the rule reads `claude_code_agents_present`, a set_fact."""
    role = applied.SETUP / "claude_code"
    wants, _ = applied_tasks.expected(role, "daniel-box", applied.context(role, "daniel-box"))  # fmt: skip
    rule = next(w for w in wants if w.path.endswith("50-claude-fanout-alert.rules"))
    assert rule.sha256 is not None and not rule.unchecked, rule
