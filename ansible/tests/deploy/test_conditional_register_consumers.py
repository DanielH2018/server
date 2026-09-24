"""Tree-wide guard for the skipped-register class.

WHY THIS EXISTS. A SKIPPED Ansible task still sets the variable it registers to. The value is
a result dict carrying `skipped: true` and no `stdout`/`rc` key at all. So a consumer that
dereferences `<reg>.stdout`, or loops `<reg>.results` and reads `item.stdout`, blows up with
"object of type 'dict' has no attribute 'stdout'" on exactly the runs where the producer's
`when:` was false — which are the runs nobody tests.

The `when:` half of the rule has bitten twice:

  * 2026-08-21, k8s/volume-snapshot: a retake wait registered over the first wait's genuine
    result and failed the deploy over a healthy snapshot. `test_volume_snapshot_register.py`
    is the behavioural anchor from that one, and it covers that role only.
  * 2026-08-22, k8s/claude-otel: the restart-count snapshot is gated on the manifests
    changing, but its assert and its stabilise-gate hand-off were not. A dashboards-only
    deploy — manifests unchanged — failed the play after the dashboards had already applied.

The behavioural test in `test_volume_snapshot_register.py` says a rendered-expression test
cannot catch this class, and it is right about the general case: the bug is in *when* Ansible
assigns a register. But the specific structural shape above IS statically visible, and it is
the shape both incidents took. Catching it costs one parse; catching it behaviourally costs a
stubbed end-to-end run per role.

The rule: if a task carries `when:` and a `register:`, every task that dereferences that
register must either carry the producer's condition too, or filter the skip results out of
its loop (`rejectattr('skipped', 'defined')`).

A PRODUCER NEEDS NO `when:` TO BE CONDITIONAL. Check mode skips `ansible.builtin.command` and
its siblings whatever their `when:` says, so an UNGUARDED command that registers is a skip
result on every `--check` run — the same dict with no `stdout`, reached by a different route.
`setup/k3s/tasks/coredns.yml` was the third incident (2026-09-22, #2305): `Read the live
Corefile` carries no `when:`, and `Point cluster DNS at Pi-hole first` compares
`k3s_coredns_live.stdout` in its own `when:`. Under `--check` that conditional errored before
the play reached anything else in the file. The fix was one `check_mode: false`; the class was
unguarded until #2315 widened `_producers` with `_check_mode_producers` below.

A consumer of a check-mode producer is clean when a `--check` run never reaches it — its own
`when:`, an enclosing `block:`, or the `when:` on the task that imports its file. All three
are `_check_mode.excludes_check_mode` over `_check_mode.walk_with_inherited_when` and
`_check_mode.importer_guards`, shared with
`deploy/test_retried_commands_survive_check_mode.py`, which asks the same questions of
the same tasks for the retry-loop version of this bug.

The widening found 33 consumers that predated it. They sat in an allowlist until #2323 fixed
the last of them, so the rule has no exemptions.

Three holes in that widening were drained on 2026-09-24, each with its own pair of anchors at
the foot of this file. `SKIP_MISSING` named only `stdout`/`stderr`/`rc`, and `\b` does not
break inside `stdout_lines`, so a consumer reading the `_lines` form passed (#2351).
`expressions()` dumped a `block:` wrapper's children as part of the wrapper, reporting a
child's own `failed_when` against the block's name (#2352). And `_check_mode_producers` skipped
any register the when-based rule already covered, so a consumer that repeated its producer's
`when:` was called clean even though check mode skips the producer regardless (#2353).

A fourth followed on the same day: both rules exempted the whole TASK once it registered a name
they tracked, so a task that reads a sibling's skip result and registers something of its own
was judged by neither. `test_registering_consumers.py` holds those anchors.

A fifth bounded that fourth (#2375). Judging every registering task judged its `failed_when`
and its `changed_when` too, and a task check mode SKIPS never evaluates either — Ansible
evaluates both on a result its module returned, and a skipped module returns none. So the
check-mode rule drops those two keys when the CONSUMER is itself skipped under `--check`, and
reads its module args and its `when:` as before: both are templated ahead of the skip. `until:`
is not dropped, though the issue asked for it — the retry loop evaluates against the skip
result. The when-based rule keeps everything, because its producer is skipped on a real run
where the consumer runs. Those anchors sit with the fourth's.
"""

from pathlib import Path

import pytest
import yaml
from lib import yaml_fast

from _check_mode import unguarded_deref
from _helpers import ROLES as _ROLES
from _skip_result_rule import _offenders, _task_files


_COREDNS = _ROLES / "setup" / "k3s" / "tasks" / "coredns.yml"

# Files the census must walk. Each held a consumer the #2315 widening flagged, so a glob that
# stopped reaching them would turn the per-file test green over files it never read.
_KNOWN_TASK_FILES = frozenset(
    {
        "setup/k3s/tasks/coredns.yml",
        "setup/hypervisor/tasks/guest.yml",
        "k8s/jellyfin/tasks/verify.yml",
        "k8s/media-volume/tasks/sync.yml",
        # The 2026-09-24 widenings: #2351 added `stdout_lines`/`stderr_lines`/`delta` to
        # SKIP_MISSING, #2353 stopped skipping a when-gated producer under the check-mode
        # rule. Each of these held a consumer one of the two flagged.
        "k8s/authelia/tasks/main.yml",
        "k8s/headlamp/tasks/main.yml",
        "k8s/prowlarr/tasks/main.yml",
        "k8s/tdarr/tasks/verify.yml",
        "setup/hypervisor/tasks/teardown.yml",
        "setup/k3s/tasks/longhorn.yml",
        "setup/k3s/tasks/longhorn-backup.yml",
        "setup/k3s/tasks/longhorn-weekly-shard.yml",
    }
)


@pytest.mark.parametrize(
    "path", _task_files(), ids=lambda p: str(p.relative_to(_ROLES))
)
def test_no_unguarded_consumer_of_a_conditional_register(path: Path) -> None:
    problems = _offenders(path)
    assert not problems, "\n".join(problem.message for problem in problems)


def test_the_census_walks_the_files_it_was_written_for() -> None:
    """The non-vacuity half of the per-file test above.

    The rule's own behaviour is pinned by the anchors below. This asserts the glob still
    reaches files that once held an offender, so a moved roles tree fails here by name.
    """
    walked = {path.relative_to(_ROLES).as_posix() for path in _task_files()}
    missing = sorted(_KNOWN_TASK_FILES - walked)
    assert not missing, f"_task_files() no longer reaches {missing}"


def test_the_widened_rule_would_have_caught_the_coredns_read(tmp_path: Path) -> None:
    """#2315's anchor: coredns.yml with its `check_mode: false` taken back off.

    `Read the live Corefile` carries no `when:`, so the older rule reads it as an
    unconditional producer. Check mode skips `command` regardless, and `Point cluster DNS at
    Pi-hole first` compares `k3s_coredns_live.stdout` in its own `when:` — which is how the
    `coredns` tag failed under `--check` on 2026-09-22 (#2305).
    """
    tasks = yaml_fast.safe_load(_COREDNS.read_text())
    reads = [t for t in tasks if t.get("name") == "Read the live Corefile"]
    assert len(reads) == 1, (
        "setup/k3s/tasks/coredns.yml no longer has exactly one task named 'Read the live "
        "Corefile' — it was renamed or removed, and this anchor would check nothing"
    )
    assert reads[0].pop("check_mode", None) is False, (
        "'Read the live Corefile' no longer carries `check_mode: false`. The #2305 fix was "
        "reverted and the `coredns` tag fails under `--check` again"
    )
    reverted = tmp_path / "tasks"
    reverted.mkdir()
    (reverted / "coredns.yml").write_text(yaml.safe_dump(tasks))

    flagged = {
        problem.task: problem.message
        for problem in _offenders(reverted / "coredns.yml")
        if "k3s_coredns_live" in problem.message
    }
    assert "Point cluster DNS at Pi-hole first" in flagged, (
        "the widened rule does not name the consumer that #2305 fixed by hand, so it would "
        f"not have caught the class it was written for — it flagged {sorted(flagged)}"
    )
    # The message names the PRODUCER as well as the consumer: the task an operator has to
    # edit is the read, and the failure is reported on the task that trips over it.
    assert "Read the live Corefile" in flagged["Point cluster DNS at Pi-hole first"], (
        "the failure names the consumer but not the read whose opt-out is the fix"
    )


def test_the_live_coredns_read_is_clean() -> None:
    """The accepting half: with `check_mode: false` in place, the file reports nothing."""
    assert _offenders(_COREDNS) == []


def test_the_check_finds_the_claude_otel_shape(tmp_path: Path) -> None:
    """Anchor the detector against the 2026-08-22 bug as it was actually written."""
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    bug = tasks / "main.yml"
    bug.write_text(
        "- name: Snapshot restart counts\n"
        "  ansible.builtin.command: kubectl get pods\n"
        "  register: restarts_before\n"
        "  when:\n"
        "    - manifests_render is changed\n"
        "- name: Fail if a selector matched no pods\n"
        "  ansible.builtin.assert:\n"
        "    that: item.stdout | trim | length > 0\n"
        '  loop: "{{ restarts_before.results | default([]) }}"\n'
        "  when: not ansible_check_mode\n"
    )
    problems = _offenders(bug)
    assert len(problems) == 1
    assert "restarts_before.stdout" in problems[0].message


def test_the_check_accepts_a_lazy_conditional_guard(tmp_path: Path) -> None:
    """k8s/volume-claim's shape: the deref sits behind `if <same gate> else`.

    Jinja's conditional expression is lazy, so `seed_volume_marker.rc` is never evaluated on
    the run where its producer was skipped. Demanding a `| default()` here is not a tidier
    spelling — that role measured it, and the default renders the expression True and would
    tar a long-gone source over every live volume.
    """
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    ok = tasks / "seed.yml"
    ok.write_text(
        "- name: Check the seed marker\n"
        "  when: not (seed_volume_short_circuit | bool)\n"
        "  ansible.builtin.command: kubectl exec test -f .seeded\n"
        "  register: seed_volume_marker\n"
        # Anchors the WHEN-based rule. The check-mode rule also reaches a when-gated
        # producer since #2353, and would report the same consumer for a second reason.
        "  check_mode: false\n"
        "- name: Decide whether this run copies\n"
        "  ansible.builtin.set_fact:\n"
        "    seed_volume_copying: >-\n"
        "      {{ false if (seed_volume_short_circuit | bool)\n"
        "         else (seed_volume_marker.rc != 0) }}\n"
    )
    assert _offenders(ok) == []


def test_the_lazy_guard_still_catches_an_unrelated_gate(tmp_path: Path) -> None:
    """Control: the exemption must key on the producer's OWN gate, not on any `if/else`.

    A conditional testing some other variable proves nothing about whether the producer ran,
    so the deref is still reachable on a skip and must still be reported.
    """
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    bad = tasks / "seed.yml"
    bad.write_text(
        "- name: Check the seed marker\n"
        "  when: not (seed_volume_short_circuit | bool)\n"
        "  ansible.builtin.command: kubectl exec test -f .seeded\n"
        "  register: seed_volume_marker\n"
        # Anchors the WHEN-based rule. The check-mode rule also reaches a when-gated
        # producer since #2353, and would report the same consumer for a second reason.
        "  check_mode: false\n"
        "- name: Decide whether this run copies\n"
        "  ansible.builtin.set_fact:\n"
        "    seed_volume_copying: >-\n"
        "      {{ false if (some_other_flag | bool)\n"
        "         else (seed_volume_marker.rc != 0) }}\n"
    )
    problems = _offenders(bad)
    assert len(problems) == 1
    assert "seed_volume_marker.rc" in problems[0].message


def test_the_check_accepts_a_filtered_loop(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    fixed = tasks / "main.yml"
    fixed.write_text(
        "- name: Snapshot restart counts\n"
        "  ansible.builtin.command: kubectl get pods\n"
        "  register: restarts_before\n"
        "  when:\n"
        "    - manifests_render is changed\n"
        "- name: Fail if a selector matched no pods\n"
        "  ansible.builtin.assert:\n"
        "    that: item.stdout | trim | length > 0\n"
        '  loop: "{{ restarts_before.results | default([]) '
        "| rejectattr('skipped', 'defined') | list }}\"\n"
        "  when: not ansible_check_mode\n"
    )
    assert _offenders(fixed) == []


def _write(tmp_path: Path, body: str) -> Path:
    """One task file under a `tasks/` directory, which `importer_guards` needs to walk."""
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    path = tasks / "main.yml"
    path.write_text(body)
    return path


_LINES_PRODUCER = (
    "- name: List the labelled volumes\n"
    "  ansible.builtin.command: kubectl get volumes -o jsonpath={.items[*].metadata.name}\n"
    "  changed_when: false\n"
    "  register: labelled\n"
)

# The attributes #2351 added to SKIP_MISSING. Parametrised so each one is load-bearing: the
# tree has no consumer of `stderr_lines` or `delta`, so dropping either from the tuple would
# leave the census green and every other anchor here passing.
_ADDED_ATTRS = ("stdout_lines", "stderr_lines", "delta")


def _reader(attr: str) -> str:
    return (
        "- name: Report what it found\n"
        "  ansible.builtin.debug:\n"
        f'    msg: "{{{{ labelled.{attr} }}}}"\n'
    )


def test_stdout_does_not_match_inside_stdout_lines() -> None:
    """Why `stdout_lines` needs its own SKIP_MISSING entry rather than riding on `stdout`.

    `unguarded_deref` anchors on `\\b<reg>.<attr>\\b`, and `_` is a word character, so there is
    no boundary inside `stdout_lines` for `stdout` to find. That is how the Longhorn label
    reconcilers, jellyfin's and janitorr's reports and media-volume's sync all read a skip
    result past this guard.
    """
    assert unguarded_deref("{{ labelled.stdout }}", "labelled", "stdout")
    assert not unguarded_deref("{{ labelled.stdout_lines }}", "labelled", "stdout")


@pytest.mark.parametrize("attr", _ADDED_ATTRS)
def test_the_check_flags_a_consumer_of_each_attribute_a_skip_result_lacks(
    tmp_path: Path, attr: str
) -> None:
    """#2351: an unguarded `command` producer leaves no `<attr>` for the reader below it."""
    problems = _offenders(_write(tmp_path, _LINES_PRODUCER + _reader(attr)))
    assert len(problems) == 1
    assert f"labelled.{attr}" in problems[0].message


@pytest.mark.parametrize("attr", _ADDED_ATTRS)
def test_the_check_accepts_those_reads_of_an_opted_out_producer(
    tmp_path: Path, attr: str
) -> None:
    """The accepting half: `check_mode: false` makes the read a real result under `--check`."""
    opted_out = _LINES_PRODUCER.replace(
        "  register: labelled\n", "  register: labelled\n  check_mode: false\n"
    )
    assert _offenders(_write(tmp_path, opted_out + _reader(attr))) == []


def test_a_block_is_not_flagged_for_its_own_childs_failed_when(tmp_path: Path) -> None:
    """#2352: `expressions()` drops `block`/`rescue`/`always`.

    `walk_with_inherited_when` yields the child on its own, so a wrapper that kept its
    children's text saw the child's expressions twice and reported the read against the
    BLOCK's name. Ansible never evaluates a skipped task's own `failed_when`, so the only
    read here is one that cannot happen — `setup/initial_setup/tasks/system-tuning.yml` was
    allowlisted for exactly this.
    """
    assert (
        _offenders(
            _write(
                tmp_path,
                "- name: Keep the info-level forwarding out of syslog\n"
                "  block:\n"
                "    - name: Check the rsyslog config parses\n"
                "      ansible.builtin.command: rsyslogd -N1\n"
                "      changed_when: false\n"
                "      register: rsyslog_parse\n"
                "      failed_when: rsyslog_parse.rc != 0\n",
            )
        )
        == []
    )


def test_a_block_is_still_flagged_for_a_childs_read_of_a_sibling(
    tmp_path: Path,
) -> None:
    """Control for the above: dropping the nesting keys must not blind the walk.

    A child reading a SIBLING's register is still reachable on a skip, and the problem is
    reported against the child's own name rather than the wrapper's.
    """
    problems = _offenders(
        _write(
            tmp_path,
            "- name: Keep the info-level forwarding out of syslog\n"
            "  block:\n"
            "    - name: Check the rsyslog config parses\n"
            "      ansible.builtin.command: rsyslogd -N1\n"
            "      changed_when: false\n"
            "      register: rsyslog_parse\n"
            "    - name: Report what it said\n"
            "      ansible.builtin.debug:\n"
            '        msg: "{{ rsyslog_parse.stdout }}"\n',
        )
    )
    assert [problem.task for problem in problems] == ["Report what it said"]


_GUEST_PRODUCER = (
    "- name: Read the running guest's live interface\n"
    "  when: hypervisor_staging_vm_info.stdout is search('State:\\s+running')\n"
    "  ansible.builtin.command:\n"
    "    cmd: virsh --connect qemu:///system dumpxml daniel-stage\n"
    "  changed_when: false\n"
    "  register: hypervisor_staging_vm_live_xml\n"
)
_GUEST_CONSUMER = (
    "- name: Stop a running guest whose live interface carries no egress fence\n"
    "  when:\n"
    "    - hypervisor_staging_vm_info.stdout is search('State:\\s+running')\n"
    "    - hypervisor_staging_vm_live_xml.stdout is not search('filterref')\n"
    "  ansible.builtin.command:\n"
    "    cmd: virsh --connect qemu:///system destroy daniel-stage\n"
    "  changed_when: true\n"
)


def test_a_when_gated_producer_is_still_judged_under_the_check_mode_rule(
    tmp_path: Path,
) -> None:
    """#2353: repeating the producer's `when:` is enough on a real run, not under `--check`.

    `setup/hypervisor/tasks/guest.yml` as it was written. The consumer repeats the producer's
    condition, so the when-based rule accepts it — and check mode skips the producer whatever
    its `when:` says, so against a running guest the consumer still read a skip result. The
    old `reg in when_based` early-out meant the check-mode rule never looked.
    """
    problems = _offenders(_write(tmp_path, _GUEST_PRODUCER + _GUEST_CONSUMER))
    assert len(problems) == 1
    assert "hypervisor_staging_vm_live_xml.stdout" in problems[0].message
    assert "check mode SKIPS" in problems[0].message, (
        "the when-based rule reported this, not the check-mode rule — the consumer repeats "
        "the producer's condition, so the when-based rule is supposed to accept it"
    )


def test_a_when_gated_producer_that_opts_out_of_check_mode_is_clean(
    tmp_path: Path,
) -> None:
    """The accepting half, and the fix guest.yml carries: `check_mode: false` on the read."""
    opted_out = _GUEST_PRODUCER.replace(
        "  register: hypervisor_staging_vm_live_xml\n",
        "  register: hypervisor_staging_vm_live_xml\n  check_mode: false\n",
    )
    assert _offenders(_write(tmp_path, opted_out + _GUEST_CONSUMER)) == []
