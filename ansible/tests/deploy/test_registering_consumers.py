"""The #2360 half of the skipped-register rule: a consumer that also registers.

Split out of `test_conditional_register_consumers.py`, which is at its module-length cap.
That module's docstring is the contract — the incidents, the two rules and why a static check
catches this class at all. These three anchors cover one narrowing of it: both rules used to
exempt a task from being judged as a consumer the moment it registered a name they tracked,
so a task that reads a SIBLING's skip result and registers something of its own passed both.
"""

from pathlib import Path

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
