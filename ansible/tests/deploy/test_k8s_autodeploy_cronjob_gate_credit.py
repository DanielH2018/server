"""Crediting a CronJob as gated through a `cronjob-gate` delegation.

A role that includes `k8s/cronjob-gate` with `cronjob_gate_name: <cronjob>` is credited for
exactly that CronJob, taken VERBATIM: vars set above the include count, a name mentioned
outside the include does not, and a commented-out include or a Jinja-valued name credits
nothing. The `wait`-shaped credit is in `test_k8s_autodeploy_batch_gates.py`; the synthetic
roles come from the same `widget_role` fixture.
"""

from __future__ import annotations

from pathlib import Path

from _autodeploy_batch import _batch_gated_names


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
