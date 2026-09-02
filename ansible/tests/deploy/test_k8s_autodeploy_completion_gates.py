"""What counts as a completion gate and a failure escalation, for a role that renders a batch workload.

A completion gate is a live task that blocks until the workload is terminal -- both
conditions, on a module that observes something -- and a failure escalation is a `fail` task
that actually runs. A poll naming only `complete`, a debug describing a wait, a trailing
comment arguing against one, and a dead wait are the shapes that must not be credited. The
name-level credit (`_batch_gated_names`) is in `test_k8s_autodeploy_batch_gates.py`.
"""

from __future__ import annotations

from _autodeploy_batch import _has_completion_gate, _has_failure_escalation


_POLL_BOTH_CONDITIONS = (
    "- name: Wait for the gate run\n"
    "  ansible.builtin.command:\n"
    "    cmd: k3s kubectl -n ns get job widget-deploy-gate -o jsonpath={.status.conditions}\n"
    "  register: r\n"
    "  until: >-\n"
    "    'Complete' in r.stdout or 'Failed' in r.stdout\n"
    "  retries: 30\n"
)


def test_a_poll_naming_only_complete_is_not_a_completion_gate(widget_role) -> None:
    """`_has_completion_gate` must reject the one-sided poll it exists to replace.

    A poll that waits only for `Complete` is `kubectl wait --for=condition=complete` wearing
    a different shape: with `backoffLimit: 0` a failed run settles in seconds and the poll
    then burns its whole retry budget before reporting anything. Accepting it would let
    cronjob-gate's poll be halved while `_GATING_SHARED_ROLES` stayed green — and
    `_batch_gated_names` credits every caller of that role.
    """
    assert _has_completion_gate(widget_role(_POLL_BOTH_CONDITIONS))
    assert not _has_completion_gate(
        widget_role(
            _POLL_BOTH_CONDITIONS.replace(" or 'Failed' in r.stdout", ""),
        )
    )
    # A comment naming both conditions is not a gate: the poll is read off the parsed task's
    # own `until:`, so a `#` line cannot reach it and neither can a second task's mention.
    assert not _has_completion_gate(
        widget_role(
            "# 'Complete' and 'Failed' are the terminal conditions\n"
            + _POLL_BOTH_CONDITIONS.replace(" or 'Failed' in r.stdout", ""),
        )
    )


def test_a_poll_on_a_module_that_observes_nothing_is_not_a_gate(widget_role) -> None:
    """`until:` is loop control on any module, so the poll half needs the same module rule.

    A `debug` retried until a string appears re-renders its own message and never reads the
    cluster. Crediting it would be the `debug`-describing-a-wait shape wearing loop control.
    """
    role = widget_role(
        "- name: Pretend to poll\n"
        "  ansible.builtin.debug:\n"
        "    msg: waiting\n"
        "  until: >-\n"
        "    'Complete' in r.stdout or 'Failed' in r.stdout\n"
        "  retries: 30\n",
    )
    assert not _has_completion_gate(role)


def test_a_trailing_comment_arguing_against_a_wait_is_not_a_gate(widget_role) -> None:
    """The fifth instance of this slice's running defect, pinned.

    Measured before the fix: a task carrying a TRAILING
    `# we deliberately do not use wait --for=condition=complete` satisfied
    `_has_completion_gate`, because the first branch was a whole-file substring test while the
    stripping only removed whole-line comments. configarr carried a comment of exactly that
    kind. Two shapes, closed by two different mechanisms: on a plain scalar YAML itself drops
    the comment, so reading the parsed task instead of the file text is what closes that one;
    inside a `shell: |` block scalar the `#` is literal content and YAML keeps it, so
    `_strip_comments` is what closes that one.
    """
    yaml_comment = widget_role(
        "- name: Reconcile  # we deliberately do not use wait --for=condition=complete\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl -n ns apply -f /tmp/x.yaml\n",
    )
    assert not _has_completion_gate(yaml_comment)

    shell_comment = widget_role(
        "- name: Reconcile\n"
        "  ansible.builtin.shell: |\n"
        "    k3s kubectl -n ns apply -f /tmp/x.yaml"
        "  # not wait --for=condition=complete: the Job can fail fast\n",
    )
    assert not _has_completion_gate(shell_comment)


def test_a_debug_describing_a_wait_is_not_a_completion_gate(widget_role) -> None:
    """Measured before the fix: an `ansible.builtin.debug` whose `msg:` names the wait
    satisfied `_has_completion_gate`, because `_task_command_text`'s module discipline was
    applied in `_batch_gated_names` and not here."""
    role = widget_role(
        "- name: Tell the operator what to do\n"
        "  ansible.builtin.debug:\n"
        "    msg: run wait --for=condition=complete job/widget-probe yourself\n",
    )
    assert not _has_completion_gate(role)


def test_a_dead_wait_is_not_a_completion_gate(widget_role) -> None:
    """A real wait that a normal deploy never runs gates nothing.

    `_live_tasks` applies the same liveness rules `_batch_gated_names` does, so all four dead
    constructions are rejected here too — including when they sit on an enclosing `block:`.
    """
    wait = (
        "- name: Wait\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl -n ns wait --for=condition=complete job/widget --timeout=180s\n"
    )
    assert _has_completion_gate(widget_role(wait))
    assert not _has_completion_gate(widget_role(wait + "  when: false\n"))
    assert not _has_completion_gate(widget_role(wait + "  tags: [never]\n"))
    assert not _has_completion_gate(widget_role(wait + "  tags: [config]\n"))
    assert not _has_completion_gate(
        widget_role(
            "- name: Gate block\n"
            "  when: false\n"
            "  block:\n"
            "    - name: Wait\n"
            "      ansible.builtin.command:\n"
            "        cmd: k3s kubectl -n ns wait --for=condition=complete job/widget\n",
        )
    )


def test_failure_escalation_needs_a_fail_task_that_runs(widget_role) -> None:
    """W2's half: `"ansible.builtin.fail" in text` credited a comment and a dead task.

    This is the only thing standing behind `_batch_gated_names` crediting every
    `k8s/cronjob-gate` caller, so a fail-open here reaches five promoted roles.
    """
    live = widget_role(
        "- name: Fail on a bad image\n  ansible.builtin.fail:\n    msg: bad image\n",
    )
    assert _has_failure_escalation(live)

    commented = widget_role(
        "- name: Report\n"
        "  ansible.builtin.debug:  # never ansible.builtin.fail here\n"
        "    msg: the deploy continues\n",
    )
    assert not _has_failure_escalation(commented)

    when_false = widget_role(
        "- name: Fail on a bad image\n"
        "  when: false\n"
        "  ansible.builtin.fail:\n"
        "    msg: bad image\n",
    )
    assert not _has_failure_escalation(when_false)

    tagged_never = widget_role(
        "- name: Fail on a bad image\n"
        "  tags: [never]\n"
        "  ansible.builtin.fail:\n"
        "    msg: bad image\n",
    )
    assert not _has_failure_escalation(tagged_never)
