"""The owed-host note reads a stamped file's record on a host before naming it (#7398).

The note and the task walk are the real ones, over this checkout's roles. Only the host read is
fake. Each test of the note also pins `daniel-server` in the note it expects to stay, so an empty
note cannot pass one that expects the host to stay owed. The tests that build a `demo` role check
which render shapes the walk counts, on shapes this checkout's roles do not hold. The check-timer
tests ask `holds` directly, over this checkout's roles.
"""

import hashlib

import pytest

from deploy_tools.land_lib.land_reach import remaining_setup_hosts_note
from deploy_tools.setup_lib.deployed_copies import DeployedCopies
from diagnostics.probe_lib import core
from lib.repo_paths import REPO

WITNESS = "ansible/roles/setup/initial_setup/files/loki_route_health.py"
WITNESS_LIVE = "/opt/loki-route-health/loki_route_health.py"
# The second path PR #7295 changed. No setup role ships it, so it owes nothing either way.
COMPONENT_COPY = "hosts/components/loki-route-witness/loki_route_health.py"
CRONS = "ansible/roles/setup/initial_setup/tasks/crons.yml"
DRIFT_LIB = "ansible/roles/setup/initial_setup/files/setup-drift-lib.sh"
WITNESS_SHA = hashlib.sha256((REPO / WITNESS).read_bytes()).hexdigest()
TEMPLATE = "ansible/roles/setup/initial_setup/templates/loki-read-route-health.sh.j2"
TEMPLATE_SHA = hashlib.sha256((REPO / TEMPLATE).read_bytes()).hexdigest()
FRAGMENT = "/var/lib/homelab/setup-render-manifest.d/loki-route-witness"
DRIFT_FRAGMENT = "/var/lib/homelab/setup-render-manifest.d/setup-drift-check"
CHECK_TIMER_TEMPLATES = (
    "ansible/roles/setup/common/templates/check-timer.service.j2",
    "ansible/roles/setup/common/templates/check-timer.timer.j2",
    "ansible/roles/setup/common/templates/check-timer-boot.service.j2",
)


def _sha(path):
    return hashlib.sha256((REPO / path).read_bytes()).hexdigest()


def _reader(row, reads=None):
    def read_host(plan, host):
        if reads is not None:
            reads.append((plan, host))
        return {"stat": {WITNESS_LIVE: row}, "files": {}}

    return read_host


def _fragment_reader(row):
    return lambda plan, host: {"stat": {}, "files": {FRAGMENT: row}}


def _note(files, read_host):
    return remaining_setup_hosts_note(
        files, "daniel-box", ref="HEAD", repo=REPO, read_host=read_host
    )


def test_a_host_holding_the_merge_commits_bytes_is_owed_nothing():
    row = {"status": "ok", "sha256": WITNESS_SHA}
    assert _note([WITNESS, COMPONENT_COPY], _reader(row)) == ""


def test_a_copy_only_rootq_could_compare_counts_as_held():
    row = {"status": "ok", "sha256": None, "via": "rootq", "match": True}
    assert _note([WITNESS], _reader(row)) == ""


def test_a_copy_with_other_bytes_keeps_the_host_owed():
    row = {"status": "ok", "sha256": "0" * 64}
    assert "`initial_setup` also reaches daniel-server" in _note(
        [WITNESS], _reader(row)
    )


def test_a_missing_copy_keeps_the_host_owed():
    assert "daniel-server" in _note([WITNESS], _reader({"status": "absent"}))


def test_a_host_that_cannot_be_read_stays_owed(capsys):
    def read_host(plan, host):
        raise core.Unreadable(f"could not read {host}: exit 255")

    assert "daniel-server" in _note([WITNESS], read_host)
    assert "could not read what daniel-server holds" in capsys.readouterr().err


def test_a_changed_sibling_with_no_stamped_copy_keeps_the_host_owed():
    row = {"status": "ok", "sha256": WITNESS_SHA}
    assert "daniel-server" in _note([WITNESS, CRONS], _reader(row))


def test_the_read_asks_for_the_merge_commits_hash():
    reads = []
    _note([WITNESS, COMPONENT_COPY], _reader({"status": "absent"}, reads))
    assert reads == [
        (
            {
                "stat": [WITNESS_LIVE],
                "rootq": {WITNESS_LIVE: WITNESS_SHA},
                "files": [],
            },
            "daniel-server",
        )
    ]


def test_two_stamped_files_read_each_host_once():
    """`ufw limit ssh` refuses a sixth connection in 30 seconds."""
    reads = []
    _note([WITNESS, DRIFT_LIB], _reader({"status": "absent"}, reads))
    hosts = [host for _, host in reads]
    assert "daniel-server" in hosts
    assert len(hosts) == len(set(hosts))


def test_a_template_the_host_last_rendered_at_this_source_is_owed_nothing():
    row = {"status": "ok", "text": f"{TEMPLATE} {TEMPLATE_SHA}\nother.j2 {'1' * 64}\n"}
    assert _note([TEMPLATE], _fragment_reader(row)) == ""


def test_a_template_rendered_from_another_source_keeps_the_host_owed():
    row = {"status": "ok", "text": f"{TEMPLATE} {'0' * 64}\n"}
    assert "daniel-server" in _note([TEMPLATE], _fragment_reader(row))


def test_a_missing_render_fragment_keeps_the_host_owed():
    assert "daniel-server" in _note([TEMPLATE], _fragment_reader({"status": "absent"}))


def _holds_check_timer(template, witness_text, drift_text, host="daniel-server"):
    """Does `initial_setup` read as held on a host with these two render fragments?

    `holds` over the working tree's roles, because `_note` reads the `ansible/` tree at `ref`, and
    the `gitops_deploy` line a check-timer change also earns keeps the note non-empty. A text of
    None is a fragment the host does not have.
    """
    files = {
        path: {"status": "ok", "text": text} if text else {"status": "absent"}
        for path, text in ((FRAGMENT, witness_text), (DRIFT_FRAGMENT, drift_text))
    }
    return DeployedCopies(
        [template],
        REPO / "ansible/roles/setup",
        ref="HEAD",
        repo=REPO,
        read_host=lambda plan, host: {"stat": {}, "files": files},
    ).holds("initial_setup", template, host)


def test_a_host_the_witness_stamp_skips_holds_on_the_drift_fragment_alone():
    template = CHECK_TIMER_TEMPLATES[0]
    line = f"{template} {_sha(template)}\n"
    assert _holds_check_timer(template, None, line, host="daniel-box")


def test_a_host_the_witness_stamp_skips_stays_owed_on_a_stale_drift_fragment():
    template = CHECK_TIMER_TEMPLATES[0]
    line = f"{template} {'0' * 64}\n"
    assert not _holds_check_timer(template, None, line, host="daniel-box")


@pytest.mark.parametrize("template", CHECK_TIMER_TEMPLATES)
def test_a_check_timer_template_both_fragments_show_is_held(template):
    line = f"{template} {_sha(template)}\n"
    assert _holds_check_timer(template, line, line)


@pytest.mark.parametrize(
    "witness_line, drift_line",
    [
        pytest.param("current", "other", id="the-drift-fragment-is-stale"),
        pytest.param("other", "current", id="the-witness-fragment-is-stale"),
        pytest.param("current", "", id="the-drift-fragment-predates-the-listing"),
    ],
)
def test_a_check_timer_template_one_fragment_does_not_show_stays_owed(
    witness_line, drift_line
):
    template = CHECK_TIMER_TEMPLATES[0]
    lines = {
        "current": f"{template} {_sha(template)}\n",
        "other": f"{template} {'0' * 64}\n",
        "": f"{TEMPLATE} {TEMPLATE_SHA}\n",
    }
    assert not _holds_check_timer(template, lines[witness_line], lines[drift_line])


ON_BOX_ONLY = "inventory_hostname == 'daniel-box'"


def _stamp(*tags, when="True"):
    return f"""
- name: Stamp
  tags: {list(tags)}
  when: "{when}"
  ansible.builtin.import_tasks: stamp.yml
  vars:
    stamp_render_name: demo
    stamp_render_templates: [{TEMPLATE}]
"""


def _render(*tags, src="loki-read-route-health.sh.j2", when="True"):
    return f"""
- name: Render
  tags: {list(tags)}
  when: "{when}"
  ansible.builtin.template:
    src: "{src}"
    dest: /tmp/demo
"""


def _demo_role_holds(tmp_path, tasks, entry="[]\n"):
    """Does a host with a current `demo` fragment hold `TEMPLATE`, for a role with these tasks?

    `entry` is the text of a task file `main.yml` does not reach.
    """
    (tmp_path / "demo" / "tasks").mkdir(parents=True)
    (tmp_path / "demo" / "tasks" / "stamp.yml").write_text("[]\n")
    (tmp_path / "demo" / "tasks" / "entry.yml").write_text(entry)
    (tmp_path / "demo" / "tasks" / "main.yml").write_text(tasks)
    row = {"status": "ok", "text": f"{TEMPLATE} {TEMPLATE_SHA}\n"}
    fragment = "/var/lib/homelab/setup-render-manifest.d/demo"
    return DeployedCopies(
        [TEMPLATE],
        tmp_path,
        ref="HEAD",
        repo=REPO,
        read_host=lambda plan, host: {"stat": {}, "files": {fragment: row}},
    ).holds("demo", TEMPLATE, "daniel-server")


def test_a_render_every_stamp_run_reaches_lets_the_fragment_count(tmp_path):
    assert _demo_role_holds(tmp_path, _render("a", "b") + _stamp("a"))


def test_a_render_the_stamps_narrower_tag_skips_keeps_it_owed(tmp_path):
    assert not _demo_role_holds(tmp_path, _render("a") + _stamp("a", "b"))


def test_a_render_and_its_stamp_both_gated_off_the_host_ask_nothing_of_it(tmp_path):
    tasks = _render("a", when=ON_BOX_ONLY) + _stamp("a", when=ON_BOX_ONLY)
    assert _demo_role_holds(tmp_path, tasks + _render("b") + _stamp("b"))


def test_a_render_whose_only_proving_stamp_is_gated_off_the_host_keeps_it_owed(
    tmp_path,
):
    tasks = _render("a") + _stamp("a", when=ON_BOX_ONLY)
    assert not _demo_role_holds(tmp_path, tasks + _render("b") + _stamp("b"))


def test_a_stamp_main_does_not_reach_proves_no_render(tmp_path):
    """A `tasks_from` play can run the untagged stamp under a tag that skips the render."""
    assert not _demo_role_holds(tmp_path, _render("a"), entry=_stamp())


def test_a_template_no_task_renders_keeps_it_owed(tmp_path):
    assert not _demo_role_holds(tmp_path, _stamp("a"))


def test_a_templated_src_over_a_variable_loop_counts_as_a_render(tmp_path):
    render = _render("other", src="{{ item }}.j2") + '  loop: "{{ names }}"\n'
    assert not _demo_role_holds(tmp_path, _render("a") + render + _stamp("a"))


@pytest.mark.parametrize(
    "pull",
    [
        '- ansible.builtin.import_tasks: "{{ somewhere }}/x.yml"\n',
        "- ansible.builtin.include_tasks: stamp.yml\n",
    ],
)
def test_tasks_this_walk_cannot_follow_keep_the_role_owed(tmp_path, pull):
    assert not _demo_role_holds(tmp_path, _render("a") + pull + _stamp("a"))
