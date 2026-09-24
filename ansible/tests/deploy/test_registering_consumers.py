"""The #2360 half of the skipped-register rule: a consumer that also registers.

Split out of `test_conditional_register_consumers.py`, which is at its module-length cap.
That module's docstring is the contract — the incidents, the two rules and why a static check
catches this class at all. The first three anchors cover one narrowing of it: both rules used
to exempt a task from being judged as a consumer the moment it registered a name they tracked,
so a task that reads a SIBLING's skip result and registers something of its own passed both.

The anchors below them cover what #2360 then over-reported (#2375). Judging every registering
task meant judging its `failed_when` and `changed_when` as well, and a task check mode SKIPS
never evaluates either: `TaskExecutor._execute` wraps both in `if 'skipped' not in result`.
`_check_mode_offenders` drops the two for such a consumer. Its module args, its `when:` and
its `until:` stay judged, and the when-based rule keeps everything.
"""

from pathlib import Path

import pytest

from _skip_result_rule import _offenders


def _write(tmp_path: Path, body: str) -> Path:
    """One task file under a `tasks/` directory, which `importer_guards` needs to walk."""
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    path = tasks / "main.yml"
    path.write_text(body)
    return path


_POD_LOOKUP = (
    "- name: Find the running tdarr pod\n"
    "  ansible.builtin.command:\n"
    "    cmd: >-\n"
    "      k3s kubectl get pod -l app=tdarr -o jsonpath={.items[0].metadata.name}\n"
    "  changed_when: false\n"
    "  register: tdarr_k8s_pod\n"
)
_REGISTERING_CONSUMER = (
    "- name: Prove the render node can actually be opened\n"
    "  ansible.builtin.command:\n"
    "    cmd: >-\n"
    '      k3s kubectl exec {{ tdarr_k8s_pod.stdout }} -- sh -c "echo OPEN_OK"\n'
    "  changed_when: false\n"
    "  register: tdarr_k8s_device\n"
    "  failed_when: \"'OPEN_OK' not in tdarr_k8s_device.stdout\"\n"
)


def test_a_consumer_that_registers_is_still_judged_against_a_siblings_register(
    tmp_path: Path,
) -> None:
    """#2360: the exemption is per REGISTER, not per task.

    `k8s/tdarr/tasks/verify.yml` as it was written. The pod lookup is an unguarded `command`,
    so `--check` leaves `tdarr_k8s_pod` a skip result; the exec below reads its `.stdout` in
    its own `cmd:` and also registers. Both rules used to exempt the WHOLE task the moment it
    registered something they tracked, so this read was never judged and the file was
    genuinely broken under `--check` while the guard reported nothing about it.
    """
    problems = _offenders(_write(tmp_path, _POD_LOOKUP + _REGISTERING_CONSUMER))
    assert [problem.task for problem in problems] == [
        "Prove the render node can actually be opened"
    ]
    assert "tdarr_k8s_pod.stdout" in problems[0].message


def test_a_task_is_not_judged_against_its_own_register(tmp_path: Path) -> None:
    """The accepting half, and the whole reason the exemption exists.

    Ansible never evaluates a skipped task's own `failed_when`, so a producer reading the
    register it sets cannot meet its own skip result. The `when:` here puts this register
    under both rules at once — the when-based one and, since #2353, the check-mode one — so
    both have to exempt the self-pairing.
    """
    assert (
        _offenders(
            _write(
                tmp_path,
                "- name: Read the live Corefile\n"
                "  when: k3s_coredns_managed | bool\n"
                "  ansible.builtin.command: k3s kubectl -n kube-system get cm coredns\n"
                "  changed_when: false\n"
                "  register: k3s_coredns_live\n"
                "  failed_when: k3s_coredns_live.rc not in [0, 1]\n",
            )
        )
        == []
    )


def test_a_consumer_that_registers_is_judged_under_the_when_based_rule_too(
    tmp_path: Path,
) -> None:
    """The same narrowing on the when-based rule, which had the identical early-out.

    The consumer carries a `when:` of its own, so it is a when-based producer too and the old
    early-out exempted it outright. Its producer carries `check_mode: false`, so the
    check-mode rule has nothing to say and the report here can only come from the `when:`
    half.
    """
    problems = _offenders(
        _write(
            tmp_path,
            "- name: Snapshot restart counts\n"
            "  when: manifests_render is changed\n"
            "  ansible.builtin.command: k3s kubectl get pods\n"
            "  changed_when: false\n"
            "  check_mode: false\n"
            "  register: restarts_before\n"
            "- name: Snapshot them again\n"
            "  when: k8s_restart_audit | bool\n"
            "  ansible.builtin.command:\n"
            "    cmd: >-\n"
            "      k3s kubectl get pods --field-selector metadata.name!={{ "
            "restarts_before.stdout }}\n"
            "  changed_when: false\n"
            "  register: restarts_after\n",
        )
    )
    assert [problem.task for problem in problems] == ["Snapshot them again"]
    assert "restarts_before.stdout" in problems[0].message
    assert "is gated on" in problems[0].message, (
        "the check-mode rule reported this, not the when-based rule — the producer carries "
        "`check_mode: false`, so the check-mode rule is supposed to ignore it"
    )


# The keys `_check_mode_offenders` drops for a consumer check mode skips. Parametrised so each
# is load-bearing: the tree has no consumer reading a sibling in its `changed_when`, so
# dropping that member would leave every other anchor here green.
_DROPPED_KEYS = ("failed_when", "changed_when")


def _skipped_consumer(key: str, expression: str) -> str:
    """A `command` consumer that registers, reading `expression` under `key` alone."""
    return (
        "- name: Prove the render node can actually be opened\n"
        "  ansible.builtin.command:\n"
        '    cmd: k3s kubectl exec tdarr -- sh -c "echo OPEN_OK"\n'
        "  register: tdarr_k8s_device\n"
        f"  {key}: {expression}\n"
    )


_READS_THE_SIBLING = "tdarr_k8s_pod.stdout | length == 0"


@pytest.mark.parametrize("key", _DROPPED_KEYS)
def test_a_skipped_consumer_is_not_judged_on_a_condition_it_never_evaluates(
    tmp_path: Path, key: str
) -> None:
    """#2375: check mode skips this consumer, so Ansible never evaluates `key`.

    `TaskExecutor._execute` wraps `failed_when` and `changed_when` in
    `if 'skipped' not in result`, so the read of the sibling's skip result cannot happen on
    the very run that produced it. #2360 made this reachable: before it, a task was exempt
    from being judged the moment it registered.
    """
    body = _POD_LOOKUP + _skipped_consumer(key, _READS_THE_SIBLING)
    assert _offenders(_write(tmp_path, body)) == []


def test_a_skipped_consumer_is_still_judged_on_its_until(tmp_path: Path) -> None:
    """The key #2375 asked for that must NOT be dropped.

    The retry loop sits outside that `if 'skipped' not in result` guard and evaluates against
    the skip result — which is the premise `test_retried_commands_survive_check_mode.py`
    rests on, that a `--check` run burns every retry on one. The read is reachable.
    """
    body = (
        _POD_LOOKUP + _skipped_consumer("until", _READS_THE_SIBLING) + "  retries: 3\n"
    )
    problems = _offenders(_write(tmp_path, body))
    assert [problem.task for problem in problems] == [
        "Prove the render node can actually be opened"
    ]
    assert "tdarr_k8s_pod.stdout" in problems[0].message


def test_the_same_consumer_is_still_flagged_for_the_read_in_its_cmd(
    tmp_path: Path,
) -> None:
    """The rejecting half: module args ARE templated before the skip.

    Ansible renders a task's arguments to decide what it would have done, so a read there
    errors under `--check` even though the task never runs. That is the tdarr shape #2360
    fixed, and the narrowing above must leave it flagged.
    """
    in_cmd = _skipped_consumer("failed_when", _READS_THE_SIBLING).replace(
        'cmd: k3s kubectl exec tdarr -- sh -c "echo OPEN_OK"',
        'cmd: k3s kubectl exec {{ tdarr_k8s_pod.stdout }} -- sh -c "echo OPEN_OK"',
    )
    problems = _offenders(_write(tmp_path, _POD_LOOKUP + in_cmd))
    assert [problem.task for problem in problems] == [
        "Prove the render node can actually be opened"
    ]
    assert "tdarr_k8s_pod.stdout" in problems[0].message


def test_a_consumer_that_opts_out_of_check_mode_is_still_judged_on_its_failed_when(
    tmp_path: Path,
) -> None:
    """Control: `check_mode: false` means the consumer RUNS under `--check`.

    Its module returns a result, Ansible evaluates `failed_when` against it, and the read of
    the sibling's skip result happens for real. The narrowing keys on the same opt-out the
    producer side does, so this task is judged in full.
    """
    opted_in = _skipped_consumer("failed_when", _READS_THE_SIBLING).replace(
        "  register: tdarr_k8s_device\n",
        "  register: tdarr_k8s_device\n  check_mode: false\n  changed_when: false\n",
    )
    problems = _offenders(_write(tmp_path, _POD_LOOKUP + opted_in))
    assert [problem.task for problem in problems] == [
        "Prove the render node can actually be opened"
    ]
    assert "check mode SKIPS" in problems[0].message


def test_a_consumer_check_mode_runs_is_still_judged_on_its_failed_when(
    tmp_path: Path,
) -> None:
    """Control: the narrowing keys on the MODULE, not on the presence of a `failed_when`.

    `ansible.builtin.file` implements check mode, so it returns a real result under `--check`
    and its `failed_when` is evaluated — meeting the skip result the `command` above left.
    """
    problems = _offenders(
        _write(
            tmp_path,
            _POD_LOOKUP + "- name: Touch a marker for the render probe\n"
            "  ansible.builtin.file:\n"
            "    path: /tmp/tdarr-probe\n"
            "    state: touch\n"
            f"  failed_when: {_READS_THE_SIBLING}\n",
        )
    )
    assert [problem.task for problem in problems] == [
        "Touch a marker for the render probe"
    ]
    assert "tdarr_k8s_pod.stdout" in problems[0].message


def test_the_when_based_rule_still_judges_a_skipped_consumers_failed_when(
    tmp_path: Path,
) -> None:
    """Control for the rule the narrowing must NOT reach.

    A when-gated producer is skipped on a REAL run, where this `command` consumer runs, its
    module returns, and its `failed_when` meets the skip dict. The producer carries
    `check_mode: false`, so the check-mode rule has nothing to say and the report here can
    only come from the `when:` half.
    """
    problems = _offenders(
        _write(
            tmp_path,
            "- name: Snapshot restart counts\n"
            "  when: manifests_render is changed\n"
            "  ansible.builtin.command: k3s kubectl get pods\n"
            "  changed_when: false\n"
            "  check_mode: false\n"
            "  register: restarts_before\n"
            "- name: Prove the rollout moved\n"
            "  ansible.builtin.command: k3s kubectl rollout status deploy/tdarr\n"
            "  changed_when: false\n"
            "  failed_when: restarts_before.stdout | length == 0\n",
        )
    )
    assert [problem.task for problem in problems] == ["Prove the rollout moved"]
    assert "is gated on" in problems[0].message, (
        "the check-mode rule reported this, not the when-based rule — the producer carries "
        "`check_mode: false`, so the check-mode rule is supposed to ignore it"
    )


# A `check_mode:` on a `block:` propagates to every child, and both arms above used to read
# only the task's own key (#2379). The tree has no such block, so both anchors below are
# `tmp_path` fixtures — the shape they pin is one an edit is free to introduce tomorrow.
_BLOCK_OPTS_OUT = (
    "- name: Read state that exists independently of this play\n"
    "  check_mode: false\n"
    "  block:\n"
)


def test_a_producer_inheriting_the_opt_out_from_its_block_is_not_a_skip_source(
    tmp_path: Path,
) -> None:
    """#2379, the false positive: this producer RUNS under `--check`.

    Its `check_mode: false` sits on the enclosing block rather than on itself, so its
    register carries a real result and the consumer below reads one. Reading only the task's
    own key made `_check_mode_producers` call it a skip source and report the consumer for a
    read that cannot fail.
    """
    producer = "".join(f"    {line}\n" for line in _POD_LOOKUP.splitlines())
    consumer = (
        "- name: Prove the render node can actually be opened\n"
        "  ansible.builtin.command:\n"
        '    cmd: k3s kubectl exec {{ tdarr_k8s_pod.stdout }} -- sh -c "echo OPEN_OK"\n'
        "  changed_when: false\n"
    )
    assert _offenders(_write(tmp_path, _BLOCK_OPTS_OUT + producer + consumer)) == []


@pytest.mark.parametrize("key", _DROPPED_KEYS)
def test_a_consumer_inheriting_the_opt_out_from_its_block_is_judged_on_that_key(
    tmp_path: Path, key: str
) -> None:
    """#2379, the false negative: this consumer RUNS under `--check`.

    The producer above it is an unguarded `command`, so `--check` leaves `tdarr_k8s_pod` a
    skip result. The consumer's block carries `check_mode: false`, so its module returns,
    Ansible evaluates `key` against that result, and the read of the sibling's skip result
    happens for real. Reading only the consumer's own key dropped `key` from the judged text
    and reported nothing at all.
    """
    consumer = "".join(
        f"    {line}\n"
        for line in _skipped_consumer(key, _READS_THE_SIBLING).splitlines()
    )
    problems = _offenders(_write(tmp_path, _POD_LOOKUP + _BLOCK_OPTS_OUT + consumer))
    assert [problem.task for problem in problems] == [
        "Prove the render node can actually be opened"
    ]
    assert "tdarr_k8s_pod.stdout" in problems[0].message
