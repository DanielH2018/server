"""Crediting a batch workload as gated: which `wait` and which cronjob-gate delegation count.

A Job or CronJob is never gated through `manifests_rollout`, so a role that renders one must
carry its own completion gate. These are the shapes that must and must not be credited as that
gate — every case is a mutation somebody could plausibly write, and crediting a gate that does
not run is the fail-open direction.

Synthetic roles rather than live ones on purpose: a case pinned to a real role stops being a
regression test the day that role is retired. The `widget_role` fixture builds them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _autodeploy import (
    _GATING_SHARED_ROLES,
    _K8S_ROLES,
    _auto_deployable,
    _batch_gated_names,
    _batch_templates,
    _has_completion_gate,
    _has_failure_escalation,
    _roles,
)


def test_batch_templates_sees_every_document_in_a_multi_job_template() -> None:
    """A template with two `---`-separated Jobs must yield both, not just the first.

    registry/templates/selftest-pull-job.yaml.j2 is the live instance: it renders
    registry-selftest-pull and registry-selftest-pull-agent in one file. A `_batch_templates`
    that stops at the first `name:` in the file would see only the former — an ungated
    second Job that no offender list would ever name, the same fail-open shape this whole
    task exists to close.
    """
    role = _K8S_ROLES / "registry"
    found = {
        name
        for filename, name in _batch_templates(role)
        if filename == "selftest-pull-job.yaml.j2"
    }
    assert found == {"registry-selftest-pull", "registry-selftest-pull-agent"}


def test_commented_out_wait_does_not_count_as_gated(widget_role) -> None:
    """A `wait --for=condition=complete job/<name>` inside a `#` comment must not gate.

    Synthetic rather than a live role, per R5's own standard: a fixture pins the behavior
    against a mutation instead of a role that might be retired. The vulnerable shape is a
    single-line comment — the whole `wait ... job/<name>` command on one line with a `#`
    only at its start, so nothing sits between "complete" and "job/" to break the match. A
    disabled task folded across two YAML lines (each independently `#`-prefixed) happens to
    self-defeat the same regex for an unrelated reason — the `#` on the second line lands
    between "complete" and "job/" — so that shape would pass even before this fix and is not
    the case this test needs to cover.
    """
    role = widget_role(
        "# disabled: k3s kubectl -n ns wait --for=condition=complete "
        "job/widget-probe --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_a_wait_in_a_trailing_shell_comment_does_not_count_as_gated(
    widget_role,
) -> None:
    """The W1 shape on the higher-stakes path: `_batch_gated_names`, not the shared-role check.

    A `#` inside a `shell: |` block scalar is literal content to YAML, so the command text a
    live task hands over can still carry a comment — and a `# TODO: wait
    --for=condition=complete job/<name>` in it would put that name in the gated set, clearing
    the very Job it says is not yet gated. The whole-line case above cannot reach here (a
    commented-out task never parses as a task at all), so this is the shape that needed
    `_strip_comments` on this path too.
    """
    role = widget_role(
        "- name: Apply the probe\n"
        "  ansible.builtin.shell: |\n"
        "    k3s kubectl -n ns apply -f /tmp/probe.yaml"
        "  # TODO: wait --for=condition=complete job/widget-probe --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_single_wait_naming_two_jobs_credits_both(widget_role) -> None:
    """One `wait --for=condition=complete` naming two Jobs must credit both names.

    Synthetic mirror of registry's real `wait ... job/registry-selftest-pull
    job/registry-selftest-pull-agent`, pinned independently so the guard's behavior on a
    multi-name wait doesn't depend on that role continuing to exist or stay denylisted.
    """
    role = widget_role(
        "- ansible.builtin.command:\n"
        "    cmd: >-\n"
        "      k3s kubectl -n ns wait --for=condition=complete\n"
        "      job/widget-a job/widget-b --timeout=120s\n"
    )
    assert _batch_gated_names(role) == {"widget-a", "widget-b"}


def test_a_from_cronjob_token_in_the_same_command_is_not_credited(widget_role) -> None:
    """An unrelated `job/<name>`-shaped token in the same command must not be credited.

    task-3-rulings-2.md S6: before `_WAIT_JOB_NAMES` anchored the scan to the run of tokens
    immediately after the wait flag, a `--from=cronjob/otherthing` earlier in the same
    one-liner was picked up as if the wait had named it too.
    """
    role = widget_role(
        "- name: Create and wait for widget-job\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.shell:\n"
        "    cmd: >-\n"
        "      k3s kubectl create job widget-job --from=cronjob/otherthing ;\n"
        "      k3s kubectl wait --for=condition=complete job/widget-job --timeout=60s\n"
    )
    assert _batch_gated_names(role) == {"widget-job"}


def test_a_jinja_job_name_in_a_wait_is_refused_outright(widget_role) -> None:
    """A `job/<name>` token that isn't a full literal must be refused, not truncated.

    task-3-rulings-2.md S7, the live instance: image-builder's wait names
    `job/build-{{ image_builder_name }}`. Truncating at the first non-`[\\w.-]` character used
    to credit the shorter, wrong-but-plausible literal `build-`; refusing the whole token
    credits nothing instead — still fail-closed, but honest about why.
    """
    role = widget_role(
        "- name: Wait for the build\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.command:\n"
        "    cmd: >-\n"
        "      k3s kubectl wait --for=condition=complete\n"
        "      job/build-{{ widget_name }} --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_argv_form_wait_is_credited(widget_role) -> None:
    """An `argv:` list form of the wait command must be credited, same as `cmd:`.

    task-3-rulings-2.md S8: `k8s/cronjob-gate` itself uses `argv:` for its own container-state
    read, with a comment recording that the `cmd:` form shipped broken once — so `argv:` is an
    established spelling in this codebase, and a guard that cannot read it would false-offend
    the next role that follows the precedent.
    """
    role = widget_role(
        "- name: Wait for widget-job\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.command:\n"
        "    argv:\n"
        "      - k3s\n"
        "      - kubectl\n"
        "      - wait\n"
        "      - --for=condition=complete\n"
        "      - job/widget-job\n"
        "      - --timeout=120s\n"
    )
    assert _batch_gated_names(role) == {"widget-job"}


def test_short_command_module_name_is_credited(widget_role) -> None:
    """The short `command:`/`shell:` module spellings (not just the FQCN) must be credited."""
    role = widget_role(
        "- name: Wait for widget-job\n"
        "  tags: [deploy]\n"
        "  command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == {"widget-job"}


def test_a_double_spaced_wait_command_is_still_credited(widget_role) -> None:
    """Extra whitespace inside the command string must not defeat the match."""
    role = widget_role(
        "- name: Wait for widget-job\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.command:\n"
        "    cmd: 'k3s kubectl wait  --for=condition=complete  job/widget-job --timeout=120s'\n"
    )
    assert _batch_gated_names(role) == {"widget-job"}


def test_batch_templates_sees_quoted_and_commented_kind(widget_role) -> None:
    """`kind: "Job"` and `kind: Job  # comment` must both be seen as batch templates.

    Both are valid YAML that kubectl applies identically to the bare `kind: Job` form. No
    live template uses either spelling today, so this is prophylactic — pinned here rather
    than left to a mutation that was only ever run by hand.
    """
    role = widget_role(
        templates={
            "quoted-job.yaml.j2": 'apiVersion: batch/v1\nkind: "Job"\nmetadata:\n  name: widget-quoted\n',
            "commented-job.yaml.j2": "apiVersion: batch/v1\nkind: Job  # one-shot\nmetadata:\n  name: widget-commented\n",
        },
    )
    found = dict(_batch_templates(role))
    assert found == {
        "quoted-job.yaml.j2": "widget-quoted",
        "commented-job.yaml.j2": "widget-commented",
    }


def test_cronjob_gate_delegation_credits_the_named_cronjob(widget_role) -> None:
    """An `include_role: k8s/cronjob-gate` credits `cronjob_gate_name`, verbatim.

    Verbatim is the whole point. The Job the shared role creates is `<name>-deploy-gate`, but
    what `_batch_templates` yields for the caller is the CronJob's own `metadata.name` — so
    crediting the Job's name would produce a string no rendered manifest can equal, gating
    nothing while reporting the role as gated. That is the shape a delegation marker took in
    this same slice before it was deleted; this test is what stops it coming back.
    """
    role = widget_role(
        "- name: Gate the widget deploy on a one-off run\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.include_role:\n"
        "    name: k8s/cronjob-gate\n"
        "  vars:\n"
        "    cronjob_gate_name: widget\n"
    )
    assert _batch_gated_names(role) == {"widget"}


def test_cronjob_gate_vars_above_the_include_are_credited(widget_role) -> None:
    """`vars:` written above `ansible.builtin.include_role:` is the same task and must count.

    Valid YAML, and Ansible runs it identically — mapping keys are unordered. Reading forward
    from the include's own `name:` line saw nothing after it and called the role ungated, which
    would tell a maintainer to add an include the role already has. Scoping to the whole task
    removes the ordering assumption rather than documenting it.
    """
    role = widget_role(
        "- name: Gate the widget deploy on a one-off run\n"
        "  tags: [deploy]\n"
        "  vars:\n"
        "    cronjob_gate_name: widget\n"
        "  ansible.builtin.include_role:\n"
        "    name: k8s/cronjob-gate\n"
    )
    assert _batch_gated_names(role) == {"widget"}


def test_cronjob_gate_name_outside_the_include_is_not_credited(widget_role) -> None:
    """`cronjob_gate_name` set anywhere but inside a k8s/cronjob-gate include gates nothing.

    A set_fact, a defaults entry, or the same var handed to some other role all mention the
    name without any gate running. Anchoring the lookup to the include's own body is what
    keeps a mention from reading as a mechanism.
    """
    role = widget_role(
        "- name: Remember what we would gate\n"
        "  ansible.builtin.set_fact:\n"
        "    cronjob_gate_name: widget\n"
    )
    assert _batch_gated_names(role) == set()


def test_commented_out_cronjob_gate_include_does_not_credit(tmp_path: Path) -> None:
    """A gate commented out to skip a slow run must go red, not stay credited.

    The same fail-open direction `test_commented_out_wait_does_not_count_as_gated` pins for
    the wait form. Commenting a task out is the likelier edit than deleting it, so it is the
    mutation worth pinning.

    Two shapes, both rejected by parsing rather than by text-matching. A wholly commented block
    is not data at all once parsed — `yaml.safe_load` on an all-`#` file yields `None`, so the
    task loop below never runs. The PARTIAL comment is the one that mattered under the old
    text-scanning approach: disabling only the include's `name:` line left the `vars:` block
    underneath as live text an unstripped raw-text read would still credit. Parsed as YAML,
    `ansible.builtin.include_role:` with a commented-out value is simply a key mapped to
    `None` — not a dict — so `isinstance(include, dict)` is False and the task is never read as
    a cronjob-gate include at all, with no gate running.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    tasks = role / "tasks" / "main.yml"
    tasks.write_text(
        "# - name: Gate the widget deploy on a one-off run\n"
        "#   ansible.builtin.include_role:\n"
        "#     name: k8s/cronjob-gate\n"
        "#   vars:\n"
        "#     cronjob_gate_name: widget\n"
    )
    assert _batch_gated_names(role) == set()

    tasks.write_text(
        "- name: Gate the widget deploy on a one-off run\n"
        "  ansible.builtin.include_role:\n"
        "#     name: k8s/cronjob-gate\n"
        "  vars:\n"
        "    cronjob_gate_name: widget\n"
    )
    assert _batch_gated_names(role) == set()


def test_a_jinja_cronjob_gate_name_is_not_credited(widget_role) -> None:
    """A templated `cronjob_gate_name` can't be resolved here, so it counts as ungated.

    Same fail-closed choice `_deployment_name` makes for a Jinja Deployment name: guessing
    which CronJob a `{{ ... }}` resolves to is how a guard credits a workload nothing waits
    on. The role reads as an offender instead, which an operator can then answer.
    """
    role = widget_role(
        "- name: Gate the widget deploy on a one-off run\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.include_role:\n"
        "    name: k8s/cronjob-gate\n"
        "  vars:\n"
        '    cronjob_gate_name: "{{ widget_cronjob }}"\n'
    )
    assert _batch_gated_names(role) == set()


def test_a_debug_message_describing_a_wait_does_not_count(widget_role) -> None:
    """A `debug` task whose message merely describes a wait must not credit its Job.

    R1, and the finding that matters most: this needs no sabotage, only plausible prose — a
    comment-shaped instruction telling the operator to run the wait by hand. A `debug` module
    runs nothing; `_task_command_text` only reads `ansible.builtin.command`/`.shell`, so this
    task contributes no command text at all regardless of its `tags:`/`when:`.
    """
    role = widget_role(
        "- name: Note the manual gate\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.debug:\n"
        "    msg: >-\n"
        "      run kubectl wait --for=condition=complete job/widget-job yourself\n"
    )
    assert _batch_gated_names(role) == set()


def test_a_wait_inside_a_when_false_block_does_not_count(widget_role) -> None:
    """A wait task must not credit its Job when the FALSEY `when:` sits on the enclosing block.

    task-3-rulings-2.md S1: a raw top-level walk finds the nested task but does not carry the
    block's own `when:`/`tags:` down to it, so all four R1 constructions credit again the
    moment they sit on the `block:` instead of the task. This pins the `when: false` case.
    """
    role = widget_role(
        "- name: Gate group\n"
        "  when: false\n"
        "  block:\n"
        "    - name: Wait for widget-job\n"
        "      tags: [deploy]\n"
        "      ansible.builtin.command:\n"
        "        cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_a_wait_inside_a_never_tagged_block_does_not_count(widget_role) -> None:
    """S1: the `tags: [never]` case, with the tag on the enclosing block."""
    role = widget_role(
        "- name: Gate group\n"
        "  tags: [never]\n"
        "  block:\n"
        "    - name: Wait for widget-job\n"
        "      ansible.builtin.command:\n"
        "        cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_a_wait_inside_a_config_tagged_block_does_not_count(widget_role) -> None:
    """S1: the `tags: [config]` case, with the tag on the enclosing block rather than the task.

    The inner task carries no tags of its own at all — its effective tags come entirely from
    the block, which is exactly the shape a bare per-task tag check misses.
    """
    role = widget_role(
        "- name: Gate group\n"
        "  tags: [config]\n"
        "  block:\n"
        "    - name: Wait for widget-job\n"
        "      ansible.builtin.command:\n"
        "        cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_a_nested_block_when_false_propagates_two_levels_down(widget_role) -> None:
    """S1: a block inside a block must still propagate a falsey `when:` to the innermost task.

    `_iter_task_dicts` accumulates as it descends rather than reading only the immediate
    parent, so this is the case that would catch a merge that stopped one level too soon.
    """
    role = widget_role(
        "- name: Outer group\n"
        "  when: false\n"
        "  block:\n"
        "    - name: Inner group\n"
        "      block:\n"
        "        - name: Wait for widget-job\n"
        "          tags: [deploy]\n"
        "          ansible.builtin.command:\n"
        "            cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def _wait_task(header: str) -> str:
    """A tasks/main.yml holding one wait on widget-job, under the given `when:`/`tags:` lines."""
    return (
        "- name: Wait for widget-job\n" + header + "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )


# Whether a wait runs at all, decided from its `when:` and `tags:` alone. Every falsy spelling
# below is one Ansible skips, so crediting it would report a role as gated while nothing ran —
# the fail-open direction. The last two rows are the controls: without them a check that simply
# refused every `when:` would pass this whole table.
_LIVENESS_CASES = [
    # task-3-rulings.md R1: the text-matching predecessor read no `when:` at all, so a wait
    # disabled as a temporary "skip this slow probe" edit still granted the exemption.
    ("  tags: [deploy]\n  when: false\n", set(), "when-false"),
    # S2: the string spellings skip exactly like the literal boolean.
    ('  tags: [deploy]\n  when: "false"\n', set(), "when-string-false"),
    ("  tags: [deploy]\n  when: 'no'\n", set(), "when-no"),
    ("  tags: [deploy]\n  when: 'False'\n", set(), "when-capital-false"),
    ("  tags: [deploy]\n  when: 0\n", set(), "when-integer-zero"),
    # S2: a `when:` list ANDs its entries, so one falsy entry skips the task whatever else is
    # in the list.
    ('  tags: [deploy]\n  when:\n    - "false"\n', set(), "when-list-with-falsy"),
    # R1: `never` is Ansible's own reserved exclusion tag — it runs only when a play asks for
    # it by name, which an auto-deploy never does.
    ("  tags: [never]\n", set(), "tagged-never"),
    # R1: every real gate task here is `tags: [deploy]`. `--skip-tags deploy` is a documented
    # invocation that skips the apply-and-wait pair, so a wait tagged only `config` would read
    # the role as gated under a run where nothing was applied for it to wait on.
    ("  tags: [config]\n", set(), "tagged-config"),
    # The controls. A real Jinja condition cannot be evaluated statically, so it is left alone
    # rather than guessed at, and a plain deploy-tagged wait is credited normally.
    (
        "  tags: [deploy]\n  when: some_condition | bool\n",
        {"widget-job"},
        "when-variable",
    ),
    ("  tags: [deploy]\n", {"widget-job"}, "no-condition"),
]


@pytest.mark.parametrize(
    "header, expected",
    [(h, e) for h, e, _ in _LIVENESS_CASES],
    ids=[i for _, _, i in _LIVENESS_CASES],
)
def test_a_wait_is_credited_only_when_it_actually_runs(
    widget_role, header, expected
) -> None:
    assert _batch_gated_names(widget_role(_wait_task(header))) == expected


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


def test_auto_deployable_roles_gate_every_batch_workload_they_render() -> None:
    """An auto-deployable role must wait on every Job/CronJob it renders.

    Without this, a batch-only role auto-deploys with no gate at all: `rollout status` has
    no Deployment to watch, `manifests_rollout: ''` skips the stability soak too, and a bad
    image is reported as a successful deploy. A role delegating its batch workload to a
    shared role (image-builder) renders no Job/CronJob template of its own, so the inner
    loop below never runs for it and the role passes vacuously — a known limit, not a hole
    an exemption branch was ever needed to close (see the module docstring).
    """
    offenders = []
    for role in _roles():
        if not _auto_deployable(role):
            continue
        gated = _batch_gated_names(role)
        for template, name in _batch_templates(role):
            if not name or name not in gated:
                offenders.append(
                    f"{role.name}: {template} renders {name or '<unnamed>'}"
                )
    assert not offenders, (
        "Auto-deployable role(s) rendering an ungated batch workload. For a Job, add a "
        "`wait --for=condition=complete job/<name>` after the apply. For a CronJob nothing "
        "runs at deploy time and no such wait can be written — include k8s/cronjob-gate with "
        "`cronjob_gate_name: <the CronJob's metadata.name>` instead, after checking that "
        "CronJob against the two properties in roles/k8s/cronjob-gate/CLAUDE.md. Or set "
        "k8s_autodeploy: false with a k8s_autodeploy_reason:\n  "
        + "\n  ".join(offenders)
    )


def test_gating_shared_roles_actually_wait() -> None:
    """Every role `_GATING_SHARED_ROLES` names must hold a real, live completion gate.

    Necessary, not sufficient. A completion gate proves the play blocks until the Job
    finishes; it does not by itself prove a failed Job fails the deploy. image-builder's own
    wait is `... || wait --for=condition=failed` with no `failed_when` on that task, so it
    exits 0 on a failed build by design — what actually fails the play is a separate
    `ansible.builtin.fail` task a few steps later. cronjob-gate is the same shape: its poll
    carries `failed_when: false` and reports nothing itself, and the escalation is the
    `ansible.builtin.fail` after it. So this checks for a gate AND an
    `ansible.builtin.fail` somewhere in the role's tasks, not that the two are wired together
    end to end.

    `failed_when` is deliberately NOT accepted as the second half: `failed_when: false` and
    `failed_when: rc != 0` are opposite meanings sharing a prefix, and a substring test
    cannot tell them apart — accepting either would let a failure *suppressor* satisfy a
    check written to prove failure escalates. Both roles here in fact carry
    `failed_when: false` on the very task that observes the outcome, so accepting it would
    have made this half of the check pass on its own inverse.

    BOTH halves walk the role's tasks through `_live_tasks` rather than searching its text.
    Neither used to. `configarr/tasks/main.yml` (not a member of this set, but the risk is
    general) explained in a comment why it deliberately did NOT use
    `kubectl wait --for=condition=complete`, and a substring check read that explanation as the
    gate it was arguing against; the same trailing-comment and prose-in-a-`debug` shapes
    satisfied the `ansible.builtin.fail` half, as did a real `fail` under `when: false` or
    `tags: [never]`. `_has_completion_gate` and `_has_failure_escalation` carry the module and
    liveness discipline now, so a comment, a `debug`, and a dead task all fail to credit.
    """
    for shared in sorted(_GATING_SHARED_ROLES):
        role = _K8S_ROLES / shared
        assert (role / "tasks/main.yml").is_file(), (
            f"{shared}: trusted as a gating shared role but has no tasks"
        )
        assert _has_completion_gate(role), (
            f"{shared}: trusted as a gating shared role but holds no completion gate — "
            "neither a `wait --for=condition=complete` nor an `until:` poll naming both "
            "the Complete and Failed conditions, on a command/shell task that a normal "
            "deploy actually runs"
        )
        assert _has_failure_escalation(role), (
            f"{shared}: waits for completion but runs no ansible.builtin.fail to actually "
            "fail the deploy on a bad image"
        )
