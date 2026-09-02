"""Is a Job or CronJob credited as gated -- the batch half of the auto-deploy derivation.

A batch workload is credited when a live task of the role waits on it by name or delegates
to `cronjob-gate`; whether the role also holds a completion gate and a failure escalation
are separate predicates the guard combines. Everything here
reads tasks through `_autodeploy._live_tasks`, so a task commented out, tagged `never` or
behind a `when: false` is invisible to the credit exactly as it is invisible to a deploy.
Consumed by `test_k8s_autodeploy_batch_gates.py` and by `_autodeploy_rollout.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

from _autodeploy import _LITERAL_NAME, _live_tasks, _strip_comments, _task_command_text


# Anchored to the run of `job/<name>` (or `job.batch/<name>`) tokens immediately following the
# wait flag (S6/task-3-rulings-2.md), not a scan of the whole command: without the anchor, an
# unrelated `job/<name>`-shaped token elsewhere in the same shell command — e.g. a `--from=
# cronjob/X` on a `kubectl create job` earlier in the same one-liner — is credited as if the
# wait named it too. `\S+` per token, not `[\w.-]+`, because a token must be validated as a
# WHOLE against `_LITERAL_NAME` before being credited (see `_batch_gated_names` below): a Jinja
# fragment like `job/build-{{ image_builder_name }}` must be refused outright, not silently
# truncated into the shorter, still-plausible-looking literal `build-`.
_WAIT_JOB_NAMES = re.compile(
    r"wait\s+--for=condition=complete\s+((?:(?:job|job\.batch)/\S+\s*)+)"
)

_JOB_NAME_TOKEN = re.compile(r"(?:job|job\.batch)/(\S+)")


def _batch_gated_names(role: Path) -> set[str]:
    """Batch workload names this role blocks on until they reach a terminal state.

    Parses tasks/main.yml as YAML rather than matching its raw text, and credits a task only
    after `_task_runs_on_a_normal_deploy` confirms — using tags/when merged down from any
    enclosing `block:` by `_iter_task_dicts` — that it actually executes. A text-matching
    version of this check was fooled by four constructions that all read as a gate without
    running one: a falsey `when:` (in any of its several spellings — see
    `_when_is_falsy_literal`), `tags: [never]`, `tags: [config]` (excluded from a
    `--skip-tags deploy` run while every real gate task here is `[deploy]`), and — the one that
    needs no sabotage, only a plausible comment — an `ansible.builtin.debug` whose message reads
    like a wait. `_task_command_text` only ever reads a real `command`/`shell` task, which
    closes that last one outright, and does so for the `argv:` spelling as well as `cmd:`.

    Two accepted forms, because a Job and a CronJob cannot be gated the same way:

    1. A role-local `wait --for=condition=complete job/<name>`, the repo's established
       pattern for a Job the role applies itself (headlamp, media-volume, netpol-baseline,
       n8n, prowlarr, registry). A single wait can name several Jobs at once — registry's
       `wait --for=condition=complete job/registry-selftest-pull
       job/registry-selftest-pull-agent --timeout=180s` — so every `job/<name>` token
       immediately following the flag is credited, not just the one adjacent to it (`_WAIT_JOB_NAMES`
       anchors the scan to that run of tokens specifically, so an unrelated `job/<name>`-shaped
       string elsewhere in the same command is never picked up). Each token is credited only if
       it is a full, literal name — `_LITERAL_NAME.match`, not a partial match — so a Jinja
       fragment like image-builder's `job/build-{{ image_builder_name }}` is refused outright
       rather than truncated into the shorter, wrong-but-plausible literal `build-`.

    2. An `include_role: k8s/cronjob-gate` with `cronjob_gate_name: <cronjob>`. A CronJob
       fires on its schedule and nothing runs at deploy time, so no `job/<name>` wait can be
       written for it. The credited name is `cronjob_gate_name` VERBATIM — the CronJob's own
       `metadata.name`, and therefore what `_batch_templates` yields for the caller's template.
       A non-literal value (a Jinja expression) is not credited, the same fail-closed choice
       `_deployment_name` makes for a non-literal Deployment name — see `_LITERAL_NAME` below,
       reused here rather than redefined. The Job the shared role creates is
       `<name>-deploy-gate`; crediting that instead would be a string no rendered manifest can
       ever equal — a marker that gates nothing, with no symptom.

    Reading the file as YAML also removes the ordering assumption a text scan needed: a caller
    writing `vars:` above `ansible.builtin.include_role:` is the same task dict either way.

    Delegating to `k8s/image-builder` is NOT credited. The Job lives in image-builder's own
    templates, not this role's, so a role that only delegates renders zero batch templates of
    its own and `test_auto_deployable_roles_gate_every_batch_workload_they_render` passes it on
    an empty loop rather than through this function. `_GATING_SHARED_ROLES` asserts that both
    shared roles really do gate what they claim to.
    """
    tasks_file = role / "tasks/main.yml"
    if not tasks_file.is_file():
        return set()
    names: set[str] = set()
    for task in _live_tasks(role):
        cmd = _task_command_text(task)
        if cmd:
            m = _WAIT_JOB_NAMES.search(_strip_comments(cmd))
            if m:
                for token in _JOB_NAME_TOKEN.findall(m.group(1)):
                    if _LITERAL_NAME.match(token):
                        names.add(token)
        include = task.get("ansible.builtin.include_role")
        if isinstance(include, dict) and include.get("name") == "k8s/cronjob-gate":
            gate_name = (task.get("vars") or {}).get("cronjob_gate_name")
            if isinstance(gate_name, str) and _LITERAL_NAME.match(gate_name):
                names.add(gate_name)
    return names


def _has_completion_gate(role: Path) -> bool:
    """Whether the role runs a live gate that blocks until a batch workload is terminal.

    Reads the role's tasks through `_live_tasks` — the same walker `_batch_gated_names` uses —
    so the four dead-gate constructions it already rejects are rejected here too: a falsey
    `when:`, `tags: [never]`, `tags: [config]`, and either of those sitting on an enclosing
    `block:`. Both accepted forms then apply the same module discipline, and that is what
    closes the shape this file keeps being caught by:

    - `kubectl wait --for=condition=complete`, read from a real `command`/`shell` task via
      `_task_command_text` and with comments stripped. A whole-file substring test credited
      both an `ansible.builtin.debug` whose `msg:` described a wait in prose and a trailing
      `# we deliberately do not use wait --for=condition=complete` — the comment configarr
      actually carried, arguing against the very thing it was read as proving.
    - an `until:` poll naming BOTH terminal conditions, which is what a role must use when the
      workload can fail fast: `wait` names one condition, so with `backoffLimit: 0` a failed
      run settles in seconds while `wait` sits for the whole timeout before reporting it. Read
      off the task dict, so it is per-task rather than file-wide (two unrelated mentions cannot
      combine) and YAML has already dropped any comment. The task must also be a command/shell
      task: `until:` is loop control available to any module, and a poll over something that
      observes nothing gates nothing. A poll naming only `Complete` is not accepted — that is
      the same one-sided wait wearing a different shape, and it is the mutation this function
      has to reject.
    """
    for task in _live_tasks(role):
        cmd = _task_command_text(task)
        if cmd is None:
            continue
        if "wait --for=condition=complete" in _strip_comments(cmd):
            return True
        until = task.get("until")
        if isinstance(until, str) and "Complete" in until and "Failed" in until:
            return True
    return False


def _has_failure_escalation(role: Path) -> bool:
    """Whether the role runs a task that can fail the deploy outright.

    Routed through `_live_tasks` for the same reason `_has_completion_gate` is: the substring
    test this replaces (`"ansible.builtin.fail" in text`) was satisfied by
    `ansible.builtin.debug:  # never ansible.builtin.fail here`, and by a real `fail` under
    `when: false` or `tags: [never]`. It is the only thing standing behind `_batch_gated_names`
    crediting every `k8s/cronjob-gate` caller, so it carries more weight than its size suggests.

    `ansible.builtin.fail` and the short `fail` Ansible also accepts, read as a module KEY on
    the task dict rather than as text anywhere in the file.
    """
    return any(
        "ansible.builtin.fail" in task or "fail" in task for task in _live_tasks(role)
    )
