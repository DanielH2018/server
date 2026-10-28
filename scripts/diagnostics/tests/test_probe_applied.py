"""Tests for `probe.py applied` (`probe_lib/applied.py` and `probe_lib/applied_tasks.py`).

Most tests walk a small role written to `tmp_path`, one task per shape the walk reads or
skips. One walks the real `claude_code` role, so a walk that stopped finding the role's units
fails by name.
"""

import hashlib
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from diagnostics.probe_lib import applied, applied_tasks
from lib.exit_codes import PROBE_FAILED, PROBE_OK, PROBE_UNREADABLE

TASKS = """\
- name: Install the script
  ansible.builtin.template: {src: x.sh.j2, dest: /usr/local/bin/x.sh, mode: "0755", owner: root, group: root}
- name: Install the secret file
  template: {src: secret.j2, dest: /etc/x/secret, mode: 0600}
- name: Copy the library
  ansible.builtin.copy: {src: lib.py, dest: /usr/local/lib/x/lib.py, mode: "0644"}
- name: Remove the retired script
  ansible.builtin.file: {path: /usr/local/bin/old.sh, state: absent}
- name: Schedule it
  ansible.builtin.cron: {name: x, cron_file: x, minute: "5", job: /usr/local/bin/x.sh, user: root}
- name: Only on the Pi
  when: inventory_hostname == 'daniel-pi'
  ansible.builtin.copy: {content: "pi\\n", dest: /etc/pi-only}
- name: Gated on a registered result
  when: probe_result.rc == 0
  ansible.builtin.copy: {content: "x", dest: /etc/registered}
- name: Make the literal directories
  ansible.builtin.file: {path: "/var/lib/x/{{ item }}", state: directory, mode: "0750"}
  loop: [a, b]
- name: Make the configured directories
  ansible.builtin.file: {path: "/var/lib/{{ item }}", state: directory}
  loop: "{{ dirs }}"
- ansible.builtin.import_tasks: extra.yml
- name: Page on failure
  ansible.builtin.import_tasks: "{{ role_path }}/../shared/alert.yml"
  vars:
    alert_name: x
- name: Copy the binary the host already has
  ansible.builtin.copy: {src: /usr/local/bin/x, dest: /opt/x/x, remote_src: true, mode: "0755"}
- name: Bring in another role
  ansible.builtin.include_role: {name: other}
"""
UNRESOLVED = """\
- name: Import a path nothing resolves
  ansible.builtin.import_tasks: "{{ some_dir }}/y.yml"
"""
SHARED = """\
- name: Install the alert unit for {{ alert_name }}
  ansible.builtin.template:
    src: "{{ role_path }}/../shared/alert.service.j2"
    dest: "/etc/systemd/system/{{ alert_name }}-alert.service"
    mode: "0644"
"""
EXTRA = """\
- name: The box's block
  when: inventory_hostname == 'daniel-box'
  block:
    - name: Write the box marker
      ansible.builtin.copy: {content: "box {{ port }}\\n", dest: /etc/x/box}
"""
GATED_FILE = """\
- name: Write the gated file
  ansible.builtin.copy: {content: gated, dest: /etc/x/gated}
"""
CTX = {"port": 8080}
SCRIPT = "#!/bin/sh\nexec x --port {{ port }}\n"


def _role(tmp_path, tasks=TASKS):
    role = tmp_path / "x"
    for sub, name, text in (
        ("tasks", "main.yml", tasks),
        ("tasks", "extra.yml", EXTRA),
        ("tasks", "gated.yml", GATED_FILE),
        ("templates", "x.sh.j2", SCRIPT),
        ("templates", "secret.j2", "token={{ x_api_token }}\n"),
        ("files", "lib.py", "print('x')\n"),
    ):
        (role / sub).mkdir(parents=True, exist_ok=True)
        (role / sub / name).write_text(text)
    _shared(tmp_path)
    return role


def _shared(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(exist_ok=True)
    (shared / "alert.yml").write_text(SHARED)
    (shared / "alert.service.j2").write_text("[Unit]\nDescription={{ alert_name }}\n")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_the_walk_reads_each_file_task_and_follows_imports_and_blocks(tmp_path):
    wants, _ = applied_tasks.expected(_role(tmp_path), "daniel-box", CTX)
    by_path = {w.path: w for w in wants}
    script = by_path["/usr/local/bin/x.sh"]
    assert script.sha256 == _sha("#!/bin/sh\nexec x --port 8080\n")
    assert (script.mode, script.owner, script.group) == ("0755", "root", "root")
    assert by_path["/usr/local/lib/x/lib.py"].sha256 == _sha("print('x')\n")
    assert by_path["/usr/local/bin/old.sh"].present is False
    assert by_path["/etc/cron.d/x"].sha256 is None
    assert by_path["/etc/x/box"].sha256 == _sha("box 8080\n")
    assert {"/var/lib/x/a", "/var/lib/x/b"} <= set(by_path)
    assert by_path["/var/lib/x/a"].mode == "0750"


def test_a_render_holding_a_secret_is_compared_by_metadata_only(tmp_path):
    wants, _ = applied_tasks.expected(_role(tmp_path), "daniel-box", CTX)
    secret = next(w for w in wants if w.path == "/etc/x/secret")
    assert secret.sha256 is None and "STUB" in secret.why_no_content
    assert secret.mode == "0600"  # YAML read the unquoted 0600 as an octal int


def test_a_false_gate_drops_the_task_and_an_unreadable_one_is_skipped(tmp_path):
    wants, skips = applied_tasks.expected(_role(tmp_path), "daniel-box", CTX)
    paths = {w.path for w in wants}
    assert "/etc/pi-only" not in paths and "/etc/registered" not in paths
    reasons = {s.task: s.reason for s in skips}
    assert (
        "reads run-time name(s) probe_result" in reasons["Gated on a registered result"]
    )
    assert "not literal" in reasons["Make the configured directories"]
    pi_wants, _ = applied_tasks.expected(_role(tmp_path), "daniel-pi", CTX)
    assert "/etc/pi-only" in {w.path for w in pi_wants}
    assert "/etc/x/box" not in {w.path for w in pi_wants}


def test_the_real_claude_code_role_yields_its_units_with_content():
    role = applied.SETUP / "claude_code"
    wants, _ = applied_tasks.expected(
        role, "daniel-box", applied.context(role, "daniel-box")
    )
    by_path = {w.path: w for w in wants}
    for unit in ("claude-rc.service", "claude-cgroup-metrics.timer"):
        want = by_path[f"/etc/systemd/system/{unit}"]
        assert want.sha256 is not None and want.mode == "0644"


def _live(want, **changes):
    row = {
        "status": "ok",
        "mode": want.mode,
        "owner": want.owner,
        "group": want.group,
        "sha256": want.sha256,
    }
    return {**row, **changes}


WANT = applied_tasks.Want("/usr/local/bin/x.sh", "t", sha256="a" * 64, mode="0755", owner="root", group="root")  # fmt: skip


@pytest.mark.parametrize(
    ("live", "status", "detail"),
    [
        (_live(WANT), "ok", "content matches"),
        (_live(WANT, sha256="b" * 64), "DRIFT", "content differs"),
        (_live(WANT, mode="0644"), "DRIFT", "mode 0644 (want 0755)"),
        ({"status": "absent"}, "DRIFT", "missing"),
        (_live(WANT, sha256=None), "ok-meta", "content unreadable as ubuntu"),
        (
            {"status": "unreadable", "error": "denied"},
            "unread",
            "cannot stat as ubuntu",
        ),
    ],
    ids=["match", "content", "mode", "missing", "root-only", "unstattable"],
)
def test_compare_names_each_outcome(live, status, detail):
    got_status, got_detail = applied.compare(WANT, live, "ubuntu")
    assert got_status == status and detail in got_detail


def test_an_absent_arm_drifts_only_when_the_path_exists():
    gone = applied_tasks.Want("/usr/local/bin/old.sh", "t", present=False)
    assert applied.compare(gone, {"status": "absent"}, "u")[0] == "ok"
    assert applied.compare(gone, _live(WANT), "u")[0] == "DRIFT"


def _run(tmp_path, rows, tasks=TASKS, ctx=CTX):
    _role(tmp_path, tasks)
    wants, _ = applied_tasks.expected(tmp_path / "x", "daniel-box", ctx)

    def read(plan, host):
        live = {w.path: rows(w) for w in wants}
        return {
            "host": host,
            "user": "ubuntu",
            "stat": {p: live[p] for p in plan["stat"]},
        }

    ns = SimpleNamespace(role="x", host="daniel-box", dry_run=False)
    return applied.run_applied(
        ns, read=read, setup=tmp_path, render_ctx=lambda role_dir, host: ctx
    )


def _as_wanted(want):
    return {"status": "absent"} if not want.present else _live(want)


def test_every_path_as_rendered_is_the_ok_verdict(tmp_path):
    assert _run(tmp_path, _as_wanted) == PROBE_OK


def test_one_differing_file_is_the_fail_verdict(tmp_path, capsys):
    def one_edited(want):
        row = _as_wanted(want)
        return (
            {**row, "sha256": "f" * 64} if want.path == "/usr/local/bin/x.sh" else row
        )

    assert _run(tmp_path, one_edited) == PROBE_FAILED
    assert "DRIFT    /usr/local/bin/x.sh  content differs" in capsys.readouterr().out


def test_a_path_this_user_cannot_stat_is_unreadable_not_ok(tmp_path):
    def cron_hidden(want):
        if want.path.startswith("/etc/cron.d/"):
            return {"status": "unreadable", "error": "[Errno 13] Permission denied"}
        return _as_wanted(want)

    assert _run(tmp_path, cron_hidden) == PROBE_UNREADABLE


def test_a_role_no_playbook_lists_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as exc:
        applied.entry_when("no_such_role", tmp_path)
    assert exc.value.code == 64


def test_a_daniel_box_only_role_runs_on_daniel_box_alone():
    """initial_setup.yml gates it on `inventory_hostname == 'daniel-box'`."""
    role = applied.SETUP / "k8s_host_files"
    assert applied.hosts_for("k8s_host_files", role) == ["daniel-box"]


def test_a_shared_import_through_role_path_is_walked_with_its_vars(tmp_path):
    wants, _ = applied_tasks.expected(_role(tmp_path), "daniel-box", CTX)
    alert = next(w for w in wants if w.path == "/etc/systemd/system/x-alert.service")
    assert alert.sha256 == _sha("[Unit]\nDescription=x\n") and alert.mode == "0644"


def test_an_import_it_cannot_resolve_is_unchecked_and_a_role_include_skipped(tmp_path):
    _, skips = applied_tasks.expected(
        _role(tmp_path, TASKS + UNRESOLVED), "daniel-box", CTX
    )
    by_task = {s.task: s for s in skips}
    lost = by_task["tasks/main.yml: import {{ some_dir }}/y.yml"]
    assert lost.unchecked and "'{{ some_dir }}/y.yml'" in lost.reason
    role_include = by_task["Bring in another role"]
    assert "another role" in role_include.reason and not role_include.unchecked


def test_an_unchecked_import_makes_an_otherwise_clean_role_partial(tmp_path, capsys):
    assert _run(tmp_path, _as_wanted, TASKS + UNRESOLVED) == PROBE_UNREADABLE
    out, err = capsys.readouterr()
    assert (
        "UNCHECKED tasks/main.yml: import {{ some_dir }}/y.yml: '{{ some_dir }}" in out
    )
    assert "coverage partial" in err


def test_every_jinja_import_in_the_real_setup_roles_resolves():
    """A census: an import the walk cannot find hides its tasks, so none may exist today.

    Only a path that does not resolve counts here. A gate or loop that reads a run-time value
    is UNCHECKED too, and `claude_code` has three by design (#6356). `claude_code` reaching
    `common/tasks/alert_unit.yml` through `{{ role_path }}` is the member
    `test_the_real_claude_code_role_walks_its_shared_alert_import` pins.
    """
    walked, lost = [], []
    for role in sorted(p for p in applied.SETUP.iterdir() if (p / "tasks" / "main.yml").is_file()):  # fmt: skip
        for host in ("daniel-box", "daniel-server"):
            _, skips = applied_tasks.expected(role, host, applied.context(role, host))
            lost += [
                f"{role.name}@{host}: {s.task}"
                for s in skips
                if s.unchecked and "render" in s.reason
            ]
        walked.append(role.name)
    assert "claude_code" in walked and len(walked) > 10
    assert lost == []


def test_the_real_node_crons_gate_resolves_for_each_host():
    """`node_crons.yml` is gated on `claude_code_artifacts_node`, which is `{{ k8s_primary_node }}`."""
    role = applied.SETUP / "claude_code"
    walked = {
        host: applied_tasks.expected(role, host, applied.context(role, host))
        for host in ("daniel-box", "daniel-server")
    }
    box_paths = {w.path for w in walked["daniel-box"][0]}
    assert {"/usr/local/bin/sync-artifacts.sh", "/usr/local/bin/claude-otel-health.sh"} <= box_paths  # fmt: skip
    for host, (_, skips) in walked.items():
        node_crons = [s for s in skips if "node_crons.yml" in s.task]
        assert all("does not apply" in s.reason for s in node_crons), (host, node_crons)
    assert any("node_crons" in s.task for s in walked["daniel-server"][1])
    # The three task files #6359 tracked read static set_facts, which the walk evaluates.
    for host, (_, skips) in walked.items():
        assert [s.task for s in skips if s.unchecked] == [], host


GATED = """\
- name: Write the ungated file
  ansible.builtin.copy: {{content: x, dest: /etc/x/ungated}}
- name: Import the gated file
  when: {when}
  ansible.builtin.import_tasks: gated.yml
"""


# fmt: off
GATE_CASES = {
    "inventory-true": ("x_enabled", {"x_enabled": True}, PROBE_OK, "ok       /etc/x/gated"),
    "inventory-false": ("x_node == inventory_hostname", {"x_node": "daniel-pi"}, PROBE_OK, "skipped  import gated.yml: its when: is false for daniel-box"),
    "false-before-a-fact": ("[x_on, 'x_facts | length > 0']", {"x_on": False}, PROBE_OK, "skipped  import gated.yml: its when: is false for daniel-box"),
    "run-time-fact": ("x_facts | length > 0", {}, PROBE_UNREADABLE, "UNCHECKED tasks/main.yml: import gated.yml: when: reads run-time name(s) x_facts"),
}
# fmt: on


@pytest.mark.parametrize(
    ("when", "ctx", "code", "line"), GATE_CASES.values(), ids=GATE_CASES
)
def test_an_import_gate_is_evaluated_against_the_host_vars(tmp_path, capsys, when, ctx, code, line):  # fmt: skip
    assert _run(tmp_path, _as_wanted, GATED.format(when=when), ctx) == code
    out = capsys.readouterr().out
    assert line in out
    assert ("UNCHECKED" in out) == (code == PROBE_UNREADABLE)


# The fact is set AFTER the gate reads it, so only a scan of the whole role can know it.
LEARN = "- name: Learn the fact\n  ansible.builtin.set_fact: {x_fact: 1}\n"
FACT_UNREAD = "UNCHECKED tasks/main.yml: import gated.yml: when: reads run-time name(s) x_fact"  # fmt: skip
# fmt: off
TYPE_TEST_CASES = {
    "fact-is-defined": ("x_fact is defined", {}, PROBE_UNREADABLE, FACT_UNREAD),
    "fact-is-none": ("x_fact is none", {}, PROBE_UNREADABLE, FACT_UNREAD),
    "fact-default": ("x_fact | default(false)", {}, PROBE_UNREADABLE, FACT_UNREAD),
    "inventory-is-defined": ("x_inv is defined", {"x_inv": None}, PROBE_OK, "ok       /etc/x/gated"),
    "inventory-is-none": ("x_inv is none", {"x_inv": None}, PROBE_OK, "ok       /etc/x/gated"),
    "inventory-default": ("x_inv | default(true)", {"x_inv": False}, PROBE_OK, "skipped  import gated.yml: its when: is false for daniel-box"),
    "defaulted-and-set": ("x_fact | default(false)", {"x_fact": True}, PROBE_UNREADABLE, FACT_UNREAD),
}
# fmt: on


@pytest.mark.parametrize(
    ("when", "ctx", "code", "line"), TYPE_TEST_CASES.values(), ids=TYPE_TEST_CASES
)
def test_a_type_test_or_filter_does_not_make_a_run_time_name_readable(tmp_path, capsys, when, ctx, code, line):  # fmt: skip
    assert _run(tmp_path, _as_wanted, GATED.format(when=when) + LEARN, ctx) == code
    out = capsys.readouterr().out
    assert line in out
    assert ("UNCHECKED" in out) == (code == PROBE_UNREADABLE)


def test_a_set_fact_name_is_not_read_from_a_default_of_the_same_name(tmp_path):
    tasks = (
        "- name: Read the list\n"
        "  ansible.builtin.command: cat /etc/x/list\n"
        "  register: x_out\n"
        "- name: Work out the list\n"
        "  ansible.builtin.set_fact: {x_list: '{{ x_out.stdout_lines }}'}\n"
        + GATED.format(when="x_list | length > 0")
    )
    _, skips = applied_tasks.expected(
        _role(tmp_path, tasks), "daniel-box", {"x_list": []}
    )
    gated = [s for s in skips if "gated.yml" in s.task]
    assert [
        (s.unchecked, s.reason.endswith("run-time name(s) x_list")) for s in gated
    ] == [(True, True)]


def test_a_remote_src_copy_is_compared_by_metadata_not_the_local_file(tmp_path):
    wants, _ = applied_tasks.expected(_role(tmp_path), "daniel-box", CTX)
    binary = next(w for w in wants if w.path == "/opt/x/x")
    assert binary.sha256 is None and "remote_src" in binary.why_no_content
    assert binary.mode == "0755"


def test_the_real_claude_code_role_walks_its_shared_alert_import():
    """claude_code reaches common/tasks/alert_unit.yml through `{{ role_path }}`."""
    role = applied.SETUP / "claude_code"
    wants, skips = applied_tasks.expected(
        role, "daniel-box", applied.context(role, "daniel-box")
    )
    named = {w.path for w in wants} | {s.task for s in skips}
    assert any("claude-fanout-server-lander-alert" in n for n in named), sorted(named)


def test_an_include_loop_walks_the_file_once_per_item_under_its_loop_var(tmp_path):
    role = _role(tmp_path)
    (role / "tasks" / "main.yml").write_text(
        "- name: Each agent\n"
        "  ansible.builtin.include_tasks: {file: agent.yml}\n"
        "  loop: '{{ agents }}'\n"
        "  loop_control: {loop_var: agent}\n"
        "  vars: {agent_name: '{{ agent.name }}'}\n"
    )
    (role / "tasks" / "agent.yml").write_text(
        "- name: Write the agent's marker\n"
        "  ansible.builtin.copy: {content: hi, dest: '/etc/{{ agent_name }}'}\n"
    )
    ctx = {"agents": [{"name": "a"}, {"name": "b"}]}
    wants, _ = applied_tasks.expected(role, "daniel-box", ctx)
    assert {w.path for w in wants} == {"/etc/a", "/etc/b"}
    _, skips = applied_tasks.expected(role, "daniel-box", {})
    assert [(s.reason, s.unchecked) for s in skips] == [("it loops over a list that is not literal", True)]  # fmt: skip
