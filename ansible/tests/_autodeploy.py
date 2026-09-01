"""How gitops_deploy decides a k8s role may auto-deploy, derived from the role sources.

Split out of test_k8s_autodeploy_guard.py, which had grown to 2,712 lines holding both this
derivation and the three test groups built on it. The logic is what the guards agree on; a
change here moves every one of them at once, which is the point — the tree-wide guards and the
synthetic-role unit tests must read a role the same way or the unit tests stop standing for
anything.

The three groups that consume it:

  * test_k8s_autodeploy_batch_gates.py — is a Job or CronJob credited as gated
  * test_k8s_autodeploy_rollout_gates.py — is a Deployment or DaemonSet credited as gated
  * test_k8s_autodeploy_guard.py — the whole-tree assertions, plus PVC and claim accounting
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from k8s_autodeploy import k8s_autodeploy_denylist
from _helpers import REPO

_REPO = REPO
_K8S_ROLES = _REPO / "ansible/roles/k8s"
# Not a workload role — the shared include every other role calls. The invariant: no role in
# _SHARED may pin an `_image:` var, because that's what makes a role Renovate-visible and
# therefore auto-deployable in the first place. Both here have no defaults/main.yml at all, so
# neither pins one — that's the supporting fact, not the rule. seed-volume pins
# seed_volume_image and does NOT belong here; it's denylisted instead and evaluated by every
# guard below like any other role.
_SHARED = {"manifests", "rollout-drain"}

# Shared roles other roles rely on to block until a batch workload is terminal. Membership
# exempts nobody from the batch guard below; the set exists so
# `test_gating_shared_roles_actually_wait` has something to check, and it proves each role
# named here really does hold a completion gate backed by a failure escalation.
#
# What belongs here: any role under roles/k8s/ whose job is to make a caller's batch workload
# observable at deploy time. image-builder applies and waits on a build Job of its own;
# cronjob-gate creates a one-off Job from the CALLER's CronJob and polls it. cronjob-gate's
# membership is load-bearing rather than documentary — `_batch_gated_names` credits a
# delegation to it, so without this assertion the poll could be gutted and every guard in this
# file would stay green while crediting a gate that no longer gates.
_GATING_SHARED_ROLES = {"image-builder", "cronjob-gate"}


def _denylist() -> set[str]:
    """The denylist as the deployer will render it — derived, not parsed.

    Reading `gitops_deploy_k8s_autodeploy_denylist` out of the deployer's defaults would
    now yield the Jinja expression as a *string*, and `set()` over a string iterates its
    characters — a denylist of single letters that silently protects nothing.
    """
    return set(k8s_autodeploy_denylist(str(_REPO / "ansible")))


def _roles() -> list[Path]:
    return sorted(
        p for p in _K8S_ROLES.iterdir() if p.is_dir() and p.name not in _SHARED
    )


# The DaemonSet-alias sweep below deliberately does NOT reuse _roles() or _SHARED: those exist
# to enumerate *deployable* roles for the auto-deploy guards above, and excluding manifests +
# rollout-drain is correct for that job. The sweep's job is different — it must see the shared
# roles too, since manifests/tasks/main.yml and rollout-drain/tasks/main.yml are two of the
# three consumers that key on the literal 'daemonset'. Kept as an independent file list so the
# two concerns can't drift into each other.
_KUBECTL_CONSUMER_ROOTS = (
    _K8S_ROLES,  # includes manifests/ and rollout-drain/, unlike _roles()
    _REPO / "ansible/post_tasks",
    _REPO / "ansible/tasks",
)

# kubectl accepts 'ds', 'daemonsets' and any casing of 'DaemonSet' as the same resource; this
# repo's convention is the exact lowercase singular 'daemonset'. Anchored on the kubectl verbs
# that take a resource-type argument, plus the `<kind>/<name>` shorthand `rollout status` and
# `rollout restart` use — neither of which ever appears in a manifest's `kind:` field, so
# `kind: DaemonSet` in a rendered manifest template is correctly never matched.
_KUBECTL_RESOURCE_ARG_RE = re.compile(
    r"\b(?:get|describe|delete|rollout\s+status|rollout\s+restart)\s+([A-Za-z]+)(?=/|\s|$)"
)
_DAEMONSET_ALIASES = {"ds", "daemonset", "daemonsets"}


def _daemonset_alias_matches(text: str) -> list[str]:
    """kubectl resource-type arguments naming DaemonSet by any spelling but exact 'daemonset'."""
    return [
        m.group(1)
        for m in _KUBECTL_RESOURCE_ARG_RE.finditer(text)
        if m.group(1).lower() in _DAEMONSET_ALIASES and m.group(1) != "daemonset"
    ]


def _kubectl_consumer_paths() -> list[Path]:
    """Every file under the roots that actually issue kubectl commands against a kind.

    Not repo-wide in the literal sense (READMEs, CI workflows, etc. are out of scope — they
    don't run kubectl), but wide enough to cover manifests/, rollout-drain/, and the post_tasks/
    and tasks/ playbooks that read a queued `kind` — the three consumers the F1 assert names.
    """
    paths = []
    for root in _KUBECTL_CONSUMER_ROOTS:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                paths.append(p)
    return paths


def _deployment_templates(role: Path) -> list[str]:
    """Templates rendering a `kind: Deployment` or `kind: DaemonSet`, by name.

    Both are gated the same way: `roles/k8s/manifests` waits on `<kind>/<name>` and the guards
    below check that every rendered workload is named in that wait. Matching only Deployment
    made a DaemonSet role render zero workloads and pass every shape guard ungated.

    The `kind` match tolerates optional quoting (`kind: "Deployment"`) and an optional trailing
    comment (`kind: Deployment  # web`), the same tolerance `_batch_templates` already carries
    for `Job`/`CronJob`. No live template uses either spelling today, but this became
    load-bearing rather than prophylactic once `_rollout_gate_offender` started trusting an
    empty result here as proof a role renders no Deployment/DaemonSet at all — a quoted or
    commented `kind:` line this couldn't see would grant a batch-gate exemption to a role that
    still has an ungated rollout.
    """
    out = []
    for t in (
        sorted((role / "templates").glob("*.j2"))
        if (role / "templates").is_dir()
        else []
    ):
        if re.search(
            r"^kind:\s*[\"']?(?:Deployment|DaemonSet)[\"']?\s*(?:#.*)?$",
            t.read_text(),
            re.MULTILINE,
        ):
            out.append(t.name)
    return out


def _batch_templates(role: Path) -> list[tuple[str, str]]:
    """Every `kind: Job` or `kind: CronJob` a template renders, as (filename, metadata.name).

    Batch workloads are gated role-locally (`wait --for=condition=complete`), not through
    `manifests_rollout`, so they need their own offender set. Matching only
    Deployment/DaemonSet made a Job-only role render zero workloads and pass every shape
    guard ungated — the same defect slice 2 found for DaemonSets.

    A template can hold several `---`-separated YAML documents, and a batch-workload
    template holding two is not hypothetical:
    `registry/templates/selftest-pull-job.yaml.j2` renders both `registry-selftest-pull`
    and `registry-selftest-pull-agent` in one file. Splitting on the document separator and
    matching `kind`/`name` within each document (rather than a single `findall` for `name:`
    over the whole file) keeps a Job's name paired with that Job, not with an unrelated
    container, volume, or a non-batch document sharing the file.

    The `kind` match tolerates an optional quoting (`kind: "Job"`) and an optional trailing
    comment (`kind: Job  # one-shot`) — both valid YAML that kubectl applies identically to
    the bare form. No template does either today, so this is prophylactic rather than fixing
    a live miss.
    """
    out: list[tuple[str, str]] = []
    tdir = role / "templates"
    for t in sorted(tdir.glob("*.j2")) if tdir.is_dir() else []:
        text = t.read_text()
        for doc in re.split(r"^---\s*$", text, flags=re.MULTILINE):
            if not re.search(
                r"^kind:\s*[\"']?(?:Job|CronJob)[\"']?\s*(?:#.*)?$", doc, re.MULTILINE
            ):
                continue
            name = re.search(r"^\s{2}name:\s*(\S+)\s*$", doc, re.MULTILINE)
            out.append((t.name, name.group(1) if name else ""))
    return out


def _strip_comments(text: str) -> str:
    """`text` with YAML/shell comments removed — whole-line AND trailing, on every line.

    A comment is the argument against a thing, and this file's whole history is text matchers
    crediting that argument as the thing itself: a commented-out wait counted as a gate, and a
    comment explaining why a role does NOT use `kubectl wait` satisfied a check for
    `kubectl wait`. Stripping only whole-line comments closed the first shape and left the
    second one open in its trailing form — measured, `_has_completion_gate` credited
    `- name: x  # we deliberately do not use wait --for=condition=complete`.

    Applied to command text as well as to file text, so it also covers a `#` comment inside a
    `shell: |` block scalar, where YAML itself does no stripping and the `#` is a real shell
    comment. `re.MULTILINE` is what makes the trailing form strip on every line of such a block
    rather than only after the last newline. An expression that legitimately contained ` #`
    would be truncated, which can only make a check stricter.
    """
    kept = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return re.sub(r"\s#.*$", "", "\n".join(kept), flags=re.MULTILINE)


_FALSY_WHEN_LITERALS = {
    # `False` alone also matches the int 0: they hash equal, so `0 in this_set` is True. A
    # literal `0` entry would be the same key written twice, not a second form covered.
    False,
    "false",
    "False",
    "FALSE",
    "no",
    "No",
    "NO",
    "off",
    "Off",
    "OFF",
    "0",
}


def _when_is_falsy_literal(when) -> bool:
    """Whether a `when:` value is a statically-decidable falsey literal, not just `when: false`.

    Ansible templates a `when:` string through Jinja and evaluates the result the same way it
    evaluates the bare boolean, so `when: "false"`, `'no'`, `'False'` and `when: 0` all skip the
    task exactly as `when: false` does — measured directly against real Ansible, not assumed.
    These are plain YAML scalars, decidable without rendering; a `when:` that references a
    variable or filter is genuine Jinja and is left alone rather than guessed at, the same
    fail-closed choice the rest of this file makes for anything it cannot resolve statically.

    A `when:` list ANDs every entry, so if it is a list, any one entry testing falsy is enough
    on its own to skip the task regardless of the others.
    """
    if isinstance(when, list):
        return any(_when_is_falsy_literal(w) for w in when)
    if isinstance(when, (bool, int, str)):
        return when in _FALSY_WHEN_LITERALS
    return False


def _normalize_tags(tags) -> tuple:
    if tags is None:
        return ()
    if isinstance(tags, str):
        return (tags,)
    return tuple(tags)


def _iter_task_dicts(
    tasks, _inherited_tags: tuple = (), _inherited_when_falsy: bool = False
) -> "list[tuple[dict, tuple, bool]]":
    """Every task in a parsed tasks/main.yml, as `(task, effective_tags, effective_when_falsy)`.

    Descends into `block`/`rescue`/`always`, and — this is the part a first pass got only half
    right — PROPAGATES the enclosing block's own `when:`/`tags:` down to every task it contains,
    the way Ansible itself does: a block's `tags:` UNION into each child's effective tags, and a
    block's `when:` ANDs with each child's own `when:`, so a falsey block `when:` (or a
    `tags: [never]`/`tags: [config]` on the block) excludes every task inside it regardless of
    what that task states on its own. A walk that finds the nested task but doesn't carry this
    down discovers the gate without discovering whether it runs — all four dead-gate
    constructions this file guards against (`when: false`, `tags: [never]`, `tags: [config]`,
    a `debug` posing as a wait) credit again the moment the construction sits on the `block:`
    instead of on the task itself, which defeats the whole point of walking into the block.
    """
    out: list[tuple[dict, tuple, bool]] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        effective_tags = _inherited_tags + _normalize_tags(task.get("tags"))
        effective_when_falsy = _inherited_when_falsy or _when_is_falsy_literal(
            task.get("when")
        )
        out.append((task, effective_tags, effective_when_falsy))
        for key in ("block", "rescue", "always"):
            nested = task.get(key)
            if isinstance(nested, list):
                out.extend(
                    _iter_task_dicts(nested, effective_tags, effective_when_falsy)
                )
    return out


def _task_runs_on_a_normal_deploy(
    effective_tags: tuple, effective_when_falsy: bool
) -> bool:
    """Whether a full, untagged deploy of this role would actually run a task with these
    effective `tags:`/`when:` — already merged down from any enclosing `block:` by
    `_iter_task_dicts`, so this function only has to apply the exclusion rules, not discover
    ancestry.

    Fail-closed: anything this can't positively confirm reads as "does not run", because a
    false exemption here is invisible while a false offender is cheap to fix.

    - a statically falsey `when:` (`_when_is_falsy_literal`) never runs, whether it sits on the
      task or on an ancestor block.
    - `tags: never`, likewise at either level, is Ansible's own reserved exclusion tag: it runs
      only when a play names it explicitly, which a normal auto-deploy never does.
    - every real gate task in this repo (headlamp, media-volume, n8n, netpol-baseline, prowlarr,
      registry, and every `k8s/cronjob-gate` include) carries `tags: [deploy]`. A task whose
      EFFECTIVE tags are non-empty but exclude `deploy` is read as excluded here, even though a
      plain `--tags <role>` run would still select it via `include_role: apply: tags:`
      mechanics — this repo also runs config-only deploys (`--skip-tags deploy`), and crediting
      a `[config]`-tagged wait as a real gate is how a task that stops running under THAT mode
      still reads as gated under this one. A task (and every enclosing block) with NO tags at
      all is not excluded by this rule: omitting tags is this repo's unremarkable default
      shape, not a signal of anything.
    """
    if effective_when_falsy:
        return False
    if "never" in effective_tags:
        return False
    if not effective_tags:
        return True
    return "deploy" in effective_tags


def _live_tasks(role: Path) -> list[dict]:
    """Every task in the role's tasks/main.yml that a normal, untagged deploy actually runs.

    The single entry point for "what does this role do", shared by `_batch_gated_names`,
    `_has_completion_gate` and `_has_failure_escalation` so the three cannot drift apart. They
    did drift: `_batch_gated_names` parsed YAML and applied the tags/`when:` rules, while the
    other two were raw substring tests over the same file — so a `debug` describing a wait, or
    a `fail` under `when: false`, credited in one and not in the other. Anything a matcher here
    wants to read about a role goes through this walker first, and the module discipline is
    then whichever module key the matcher reads off the returned dict.
    """
    tasks_file = role / "tasks/main.yml"
    if not tasks_file.is_file():
        return []
    parsed = yaml.safe_load(tasks_file.read_text())
    return [
        task
        for task, tags, when_falsy in _iter_task_dicts(parsed)
        if _task_runs_on_a_normal_deploy(tags, when_falsy)
    ]


def _task_command_text(task: dict) -> str | None:
    """The shell/command string this task runs, or None if it isn't a command/shell task.

    Only `ansible.builtin.command`/`ansible.builtin.shell` (FQCN or the short `command`/`shell`
    module names Ansible also accepts) credit a wait. An `ansible.builtin.debug` whose `msg:`
    merely describes a wait in prose — "run kubectl wait --for=condition=complete
    job/widget-job yourself" — never runs anything, and reading its text as a gate would be the
    same "argument-against read as the thing itself" shape `_uncommented` exists to close for a
    comment, wearing a module instead of a `#`.

    Reads both spellings a command/shell task can take: `cmd:` (a single string, however it was
    folded/literal-blocked in YAML — `yaml.safe_load` already normalized that) and `argv:` (a
    list of raw arguments, joined with spaces here so the same regexes downstream can read
    either form). `argv:` is not hypothetical in this codebase — `k8s/cronjob-gate` itself uses
    it for its own container-state read, specifically BECAUSE the `cmd:` form once shipped
    broken (see its tasks/main.yml). A guard that could not read the spelling this repo already
    established as the correct one would false-offend the next role that follows the precedent.
    """
    for module in (
        "ansible.builtin.command",
        "ansible.builtin.shell",
        "command",
        "shell",
    ):
        args = task.get(module)
        if isinstance(args, dict):
            cmd = args.get("cmd")
            if isinstance(cmd, str):
                return cmd
            argv = args.get("argv")
            if isinstance(argv, list) and all(isinstance(a, str) for a in argv):
                return " ".join(argv)
        elif isinstance(args, str):
            return args
    return None


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


def _sets_empty_rollout(role: Path) -> bool:
    """Whether the role passes `manifests_rollout: ''`.

    Tolerates a trailing comment (`manifests_rollout: ''  # nothing to roll`) — this repo's
    house style comments nearly every var, and a raw-text matcher requiring end-of-line right
    after the closing quote would go blind to that shape while `roles/k8s/manifests` itself
    still evaluates the same value as empty and skips the rollout entirely. Without this, both
    `_rollout_gate_offender` and the Deployment gate below would read a commented empty rollout
    as "sets no `manifests_rollout` at all" rather than "sets it to nothing."
    """
    tasks = role / "tasks/main.yml"
    if not tasks.is_file():
        return False
    return bool(
        re.search(
            r"""manifests_rollout:\s*(""|'')\s*(?:#.*)?$""",
            tasks.read_text(),
            re.MULTILINE,
        )
    )


def _rollout_gate_offender(role: Path) -> bool:
    """Whether a role with `manifests_rollout: ''` has no gate at all.

    `manifests_rollout: ''` skips the PRIMARY rollout's wait and stability soak. The general
    rule (task-3-rulings-2.md S5, generalising R2 rather than special-casing it): such a role
    is an offender UNLESS every workload it renders — Deployment, DaemonSet and batch alike —
    is gated by some mechanism this file can see, and it renders at least one workload total.

    That single rule covers three shapes that used to need three different checks:

    - A batch-only role (no Deployment/DaemonSet at all) whose rendered Jobs/CronJobs are all
      credited by `_batch_gated_names` — this is R2's original case.
    - A role that skips the PRIMARY rollout but gates its Deployment through
      `manifests_extra_rollouts` instead. Extras roll and soak independently of the primary —
      `roles/k8s/manifests/tasks/main.yml`'s extra-rollout queue task carries no
      `manifests_rollout | length > 0` condition — so `_ungated_deployments(role) == []` is
      already proof this Deployment IS gated, and an unconditional "renders a Deployment ⇒
      offender" (R2's own rule, before this generalisation) falsely accused this shape: the
      reviewer measured `_rollout_gate_offender: True` while `_ungated_deployments: []`.
    - A role rendering an ungated Deployment `_deployment_templates` can't see by itself (a
      quoted or trailing-comment `kind:` line, or a second Deployment in a `---`-split
      template) — `_ungated_deployments` already resolves each rendered Deployment's name
      against the gated set, so this shape is caught the same way an ordinary ungated
      Deployment is, without a separate "renders any Deployment at all" check.

    The condition on ALL of that is `manifests_rollout: ''`, written literally in the role's
    tasks. A role that does not write it returns False at the top and is never judged here —
    including a role that renders no workload at all, which is the part this docstring used to
    overstate. It claimed a role rendering nothing is "still an offender"; that holds only when
    the role ALSO sets `manifests_rollout: ''`. `n8n-images` is the counterexample: it renders
    no Deployment and no batch template, and it is not an offender, because it never includes
    `k8s/manifests` and so never passes a `manifests_rollout` for `_sets_empty_rollout` to find.

    So the true statement is narrower, in two parts:

    - A role that never calls `k8s/manifests` is outside this guard entirely. Its workloads, if
      any, reach the cluster some other way, and whatever gates them is not
      `manifests_rollout`. `test_auto_deployable_roles_gate_every_batch_workload_they_render`
      and `_GATING_SHARED_ROLES` are what cover that shape.
    - A role that DOES write `manifests_rollout: ''` and renders no workload is an offender.
      Rendering nothing is not evidence of a gate, only an absence of anything to check, so it
      stays fail-closed and must declare `k8s_autodeploy: false` instead.
    """
    if not _sets_empty_rollout(role):
        return False
    if _ungated_deployments(role):
        return True
    batch = _batch_templates(role)
    total_workloads = len(_deployment_templates(role)) + len(batch)
    if not total_workloads:
        return True
    gated = _batch_gated_names(role)
    return any(not name or name not in gated for _, name in batch)


def _extra_rollouts(role: Path) -> set[str]:
    """Deployment names the role gates via `manifests_extra_rollouts`.

    Parsed with a regex rather than yaml.safe_load: tasks/main.yml is Jinja-templated, and a
    role is free to build the list from a variable. A name this cannot see reads as ungated,
    which fails the guard — the safe direction.
    """
    tasks = role / "tasks/main.yml"
    if not tasks.is_file():
        return set()
    block = re.search(
        r"^\s*manifests_extra_rollouts:\s*$\n((?:\s*-\s*name:.*\n?)+)",
        tasks.read_text(),
        re.MULTILINE,
    )
    if not block:
        return set()
    return set(re.findall(r"-\s*name:\s*(\S+)", block.group(1)))


def _primary_rollout_name(role: Path) -> str:
    """The Deployment name `roles/k8s/manifests` waits on as this role's primary rollout.

    Mirrors `manifests_rollout | default(manifests_service)` from
    `roles/k8s/manifests/tasks/main.yml`. Every role that calls the shared role sets
    `manifests_service` to a literal string, and every `manifests_rollout` override is
    likewise a literal (checked repo-wide), so a regex match is safe here. A role that never
    calls k8s/manifests resolves to '', which matches no real Deployment name.

    Tolerates a trailing comment after the value, the same widening R3/`_sets_empty_rollout`
    made — without it, `manifests_rollout: ''  # nothing to roll` disagreed between the two
    matchers reading the same variable: `_sets_empty_rollout` said "empty" while this one, still
    anchored at end-of-line right after the closing quote, fell through to `manifests_service`
    and returned the real service name instead (task-3-rulings-2.md S4).
    """
    tasks = role / "tasks/main.yml"
    if not tasks.is_file():
        return ""
    text = tasks.read_text()
    rollout = re.search(
        r"""^\s*manifests_rollout:\s*(?:"([^"]*)"|'([^']*)'|(\S+))\s*(?:#.*)?$""",
        text,
        re.MULTILINE,
    )
    if rollout:
        return next(g for g in rollout.groups() if g is not None)
    service = re.search(r"^\s*manifests_service:\s*(\S+)\s*$", text, re.MULTILINE)
    return service.group(1) if service else ""


def _primary_rollout_kind(role: Path) -> str:
    """The kubectl kind `roles/k8s/manifests` will use for this role's primary rollout.

    Mirrors `manifests_rollout_kind | default('deploy')`. Same regex-is-safe reasoning as
    `_primary_rollout_name`: every caller sets this to a literal, and the shared role asserts
    the value is one of 'deploy'/'daemonset' before anything reads it.
    """
    tasks = role / "tasks/main.yml"
    if not tasks.is_file():
        return "deploy"
    match = re.search(
        r"""^\s*manifests_rollout_kind:\s*(?:"([^"]*)"|'([^']*)'|(\S+))\s*$""",
        tasks.read_text(),
        re.MULTILINE,
    )
    if not match:
        return "deploy"
    return next(g for g in match.groups() if g is not None)


_DEPLOYMENT_NAME = re.compile(
    r"^kind:\s*(?:Deployment|DaemonSet)\s*$\n\s*metadata:\s*$\n\s*name:\s*(.+?)\s*$",
    re.MULTILINE,
)
_LITERAL_NAME = re.compile(r"^[\w.-]+$")


def _deployment_name(template: Path) -> str | None:
    """The rendered Deployment's `metadata.name`, or None if it isn't a static literal.

    Every Deployment template puts `name:` two lines under `kind: Deployment` (`metadata:` in
    between) — checked against all 56 Deployment templates in the repo. A non-literal value (a
    Jinja expression, e.g. pihole's `{{ inst.name }}`) can't be resolved without rendering, so
    this returns None rather than a guess; the caller treats None as ungated, the fail-closed
    direction.
    """
    match = _DEPLOYMENT_NAME.search(template.read_text())
    if not match:
        return None
    name = match.group(1)
    return name if _LITERAL_NAME.match(name) else None


def _gated_names(role: Path) -> set[str]:
    """Deployment names this role's rollout gate actually waits on."""
    return {_primary_rollout_name(role)} | _extra_rollouts(role)


def _ungated_deployments(role: Path) -> list[str]:
    """Rendered Deployment templates whose resolved name is not in the gated set.

    Matches by identity, not count: a rendered Deployment is gated only if its own resolved
    name equals the primary rollout name or appears in manifests_extra_rollouts. A typo'd or
    drifted extra name, or a Deployment whose name can't be resolved statically, both count as
    ungated — count alone let a mismatched name through as long as the totals lined up.
    """
    gated = _gated_names(role)
    return [
        template
        for template in _deployment_templates(role)
        if _deployment_name(role / "templates" / template) not in gated
    ]


def _ungated_deployment_count(role: Path) -> int:
    return len(_ungated_deployments(role))


def _auto_deployable(role: Path) -> bool:
    """Whether gitops_deploy may auto-deploy this role, per the role's own declaration.

    Fail-closed: a role that declares nothing is not auto-deployable. The completeness guard
    makes that unreachable for a role that declares, and it is still the right default for a
    role that doesn't.

    Reads defaults/main.yml as a plain FILE via yaml.safe_load, not as a live Ansible variable.
    Whoever re-points gitops_deploy at these declarations (slice 1b) must do the same: k8s_autodeploy
    and k8s_autodeploy_reason are unprefixed keys, shared by name across every role in one play, so
    Ansible variable lookup would resolve whichever role's defaults last set them in load order —
    not the role being asked about.
    """
    defaults = role / "defaults/main.yml"
    if not defaults.is_file():
        return False
    data = yaml.safe_load(defaults.read_text()) or {}
    return bool(data.get("k8s_autodeploy"))


_MANIFEST_KIND_TO_ROLLOUT_KIND = {"Deployment": "deploy", "DaemonSet": "daemonset"}


def _deployments_missing_readiness_probe(role: Path) -> list[str]:
    """Rendered Deployment template names with no readinessProbe.

    Was `any()` across the role's templates, so a probe on the primary Deployment satisfied
    the whole role and a probe-less *extra* passed unchecked — exactly the gap
    `manifests_extra_rollouts` opened. Checks every rendered Deployment individually instead.
    """
    return [
        name
        for name in _deployment_templates(role)
        if "readinessProbe" not in (role / "templates" / name).read_text()
    ]


# A test inferring a denylist entry's justification from the role's rendered shape (gated
# extras => the "ungated sub-deployment" reason is stale) can't tell that reason apart from a
# different one that happens to leave the same shape — prowlarr and freshrss are gated AND
# still denylisted, for migrating-state reasons this file's shape-only helpers can't see. A
# reason-aware version of this check lands with the per-role k8s_autodeploy_reason declaration.


def _declares_autodeploy(role: Path) -> bool:
    defaults = role / "defaults/main.yml"
    if not defaults.is_file():
        return False
    data = yaml.safe_load(defaults.read_text()) or {}
    return "k8s_autodeploy" in data and bool(
        str(data.get("k8s_autodeploy_reason", "")).strip()
    )


# ── slice 7a task 3: k8s_autodeploy_snapshot_pvcs ────────────────────────────────────────────
#
# k8s/manifests takes a pre-apply Longhorn snapshot of a role's `k8s_autodeploy_snapshot_pvcs`
# claims (roles/k8s/manifests/tasks/main.yml, guarded on `not k8s_no_mutate`). That guard means
# --dry-run never exercises volume-snapshot at all — a typo'd claim name is invisible to a dry
# run and surfaces only on a real deploy, as the "PVC has no spec.volumeName" assert failing
# before the apply (task-2-report.md). Test 1 below is therefore the only pre-deploy catch.
#
# THE BRIEF'S RECIPE FOR TEST 1 DOESN'T MATCH THIS CODEBASE'S SHAPE, AND HAD TO BE ADAPTED.
# The brief says: parse the role's own `templates/*.j2` for `kind: PersistentVolumeClaim` and
# assert every declared claim's name appears. Checked against the live tree
# (`grep -rl PersistentVolumeClaim ansible/roles/k8s/{home-assistant,sonarr,radarr,jellyfin,
# qbittorrent,bazarr,prowlarr,freshrss,livesync,speedtest,tdarr,code-server}`): only
# code-server/templates/pvc-workspace.yaml.j2 matches. Eleven of the thirteen roles delegate PVC
# creation entirely to the shared `k8s/seed-volume` role (`include_role: k8s/seed-volume`,
# `vars: seed_volume_claim: "{{ <role>_k8s_claim }}"`) and render no PersistentVolumeClaim
# document of their own at all. The literal recipe would find an empty rendered set for those
# eleven and fail every one of their correct declarations — not vacuous, wrong. And the two
# roles that DO render their own PVC (zigbee2mqtt, code-server's workspace claim) still write
# `metadata.name: {{ <role>_k8s_claim }}`, a Jinja reference, not a literal — so even those two
# need the same resolution step. `_rendered_pvc_claims` below is the adapted version: it reads
# both sources (a role's own PVC template AND a `seed_volume_claim` var on a live
# `k8s/seed-volume` include) and resolves the single-var-reference shape both use through the
# role's own defaults/main.yml. This is a finding about the brief, not about any role's claim
# name — every claim in the table was independently verified against `kubectl -n homelab get
# pvc` on 2026-08-21 and all fourteen are live.

_PVC_KIND = re.compile(
    r"^kind:\s*[\"']?PersistentVolumeClaim[\"']?\s*(?:#.*)?$\n\s*metadata:\s*$\n",
    re.MULTILINE,
)


def _pvc_names(text: str) -> list[str]:
    """`metadata.name` for every `kind: PersistentVolumeClaim` document in `text`.

    Reads the metadata block by indentation rather than assuming `name:` is the line
    immediately after `metadata:` — the earlier regex required exactly that, so a PVC whose
    metadata carried `labels:` before `name:` yielded no claim and no complaint (R6). A key at
    the same indentation as the block's first key is a sibling of `name:`; a shallower
    indentation means the metadata block ended.

    Two limits this still does not close, both real and left as read-the-tree-by-eye cases
    rather than chased into a renderer:

    - A PVC document nested inside `{% if ... %}` is credited whether or not that condition is
      ever true.
    - A `.j2` file left in the tree after being dropped from `manifests_files` (the list
      `roles/k8s/manifests` actually applies) is credited too — this glob has no notion of
      "still deployed".

    So this predicate is not fail-closed: it is fail-open in both of those shapes, and
    fail-closed only against the metadata-ordering gap R6 fixed.
    """
    names = []
    for kind_match in _PVC_KIND.finditer(text):
        indent = None
        for line in text[kind_match.end() :].splitlines():
            if not line.strip():
                continue
            this_indent = len(line) - len(line.lstrip(" \t"))
            if indent is None:
                indent = this_indent
            elif this_indent < indent:
                break
            if this_indent == indent:
                name = re.match(r"name:\s*(.+?)\s*$", line.strip())
                if name:
                    names.append(name.group(1))
                    break
    return names


# The only PVC-name shape this repo writes: a whole-field reference to exactly one role-local
# var — never a literal string, never a compound expression (`{{ a }}-{{ b }}`), never a
# filter. `_resolve_claim_token` refuses anything else rather than guessing at it.
_SINGLE_VAR_REF = re.compile(r"^\{\{\s*(\w+)\s*\}\}$")


def _role_defaults(role: Path) -> dict:
    """`role`'s defaults/main.yml as a plain dict, or `{}` if it has none."""
    defaults = role / "defaults/main.yml"
    if not defaults.is_file():
        return {}
    return yaml.safe_load(defaults.read_text()) or {}


def _resolve_claim_token(token: str, defaults: dict) -> str | None:
    """A PVC-name token, resolved to the literal claim name it renders — or None if it can't be.

    `token` is either already a literal (`_LITERAL_NAME`, this file's existing standard for "no
    Jinja left to resolve") or the single-var shape `_SINGLE_VAR_REF` matches, looked up in the
    role's OWN defaults. A var absent from defaults, a non-string value, or a value that isn't
    itself a literal (chained Jinja) all return None — the caller's job, not this function's, is
    to decide whether None means "report as unresolvable" or "count as not rendered".
    """
    token = token.strip()
    if _LITERAL_NAME.match(token):
        return token
    match = _SINGLE_VAR_REF.match(token)
    if match:
        value = defaults.get(match.group(1))
        if isinstance(value, str) and _LITERAL_NAME.match(value):
            return value
    return None


def _rendered_pvc_claims(role: Path) -> tuple[set[str], list[str]]:
    """PVC claim names `role` actually causes to exist, as `(resolved, unresolved_tokens)`.

    Two sources, because this repo builds a PVC two different ways:

    1. A `kind: PersistentVolumeClaim` document in the role's own `templates/*.j2` —
       zigbee2mqtt's data claim and code-server's workspace claim are the only two.
    2. A `vars: seed_volume_claim: ...` on a task that includes `k8s/seed-volume` — how the
       other twelve claims are actually created. Read through `_live_tasks`, the same walker
       `_batch_gated_names` uses, so a commented-out or `when: false`-gated include credits
       nothing, the same "argument-against read as the thing itself" trap this file's other
       matchers are written against.

    Every token found by either path is resolved through `_resolve_claim_token`. A token that
    doesn't resolve is returned UNCHANGED in the second element rather than dropped — dropping
    it would silently pass a role whose claim var was renamed or removed out from under a live
    declaration, which is the same shape as crediting a comment, this time by omission.
    """
    defaults = _role_defaults(role)
    raw: list[str] = []

    tdir = role / "templates"
    for t in sorted(tdir.glob("*.j2")) if tdir.is_dir() else []:
        raw.extend(_pvc_names(t.read_text()))

    for task in _live_tasks(role):
        include = task.get("ansible.builtin.include_role")
        if isinstance(include, dict) and include.get("name") == "k8s/seed-volume":
            claim = (task.get("vars") or {}).get("seed_volume_claim")
            if isinstance(claim, str):
                raw.append(claim)

    resolved: set[str] = set()
    unresolved: list[str] = []
    for token in raw:
        name = _resolve_claim_token(token, defaults)
        if name is not None:
            resolved.add(name)
        else:
            unresolved.append(token)
    return resolved, unresolved


_STRATEGY_RECREATE = re.compile(
    r"^\s*strategy:\s*$\n\s*type:\s*Recreate\s*$", re.MULTILINE
)


def _deployment_strategy_is_recreate(role: Path) -> bool:
    """Whether any template `role` renders declares `strategy: / type: Recreate`.

    Comments stripped first so a `# type: Recreate` mentioned in passing (a rationale comment
    on a RollingUpdate role explaining why it ISN'T Recreate, say) can't be credited — the same
    discipline `_strip_comments` exists to enforce everywhere else in this file.
    """
    tdir = role / "templates"
    if not tdir.is_dir():
        return False
    return any(
        _STRATEGY_RECREATE.search(_strip_comments(t.read_text()))
        for t in sorted(tdir.glob("*.j2"))
    )


def _migrating_state(role: Path) -> bool:
    """Whether `role` has the shape volume-snapshot exists for: `strategy: Recreate` against at
    least one rendered RWO PVC claim.

    This is the mechanical definition, read off what the role actually renders — NOT off
    `k8s_autodeploy_reason` text.

    Measured 2026-08-21: this predicate is true for 31 roles, not the thirteen slice 7a task 3
    declared `k8s_autodeploy_snapshot_pvcs` for. `_migrating_state` is broad on purpose — it
    reads `strategy: Recreate` plus a rendered RWO claim off every role, whether or not that
    role is auto-deployable. Before slice 7b task 7 promoted twelve of those thirteen, the 31
    roles this predicate flagged and the 14 `_auto_deployable` roles did not intersect at all,
    which is what made `test_auto_deployable_migrating_state_roles_declare_snapshot_pvcs` below
    vacuous. Task 7 made the two sets overlap on those twelve; the same-day scope decision then
    re-denied three of them (zigbee2mqtt, livesync, qbittorrent — state coupled outside the
    volume, not a snapshot gap), and a later audit re-denied tdarr for the same reason — so the
    overlap the guard actually exercises today is the remaining eight. Every count along the way
    is non-empty, so the guard bites instead of matching an empty loop.

    Almost every PVC `_rendered_pvc_claims` can find in this repo hardcodes
    `accessModes: [ReadWriteOnce]` (both direct templates and k8s/seed-volume's shared one), so a
    rendered claim existing at all is normally sufficient without a separate accessModes read.
    The one exception: `k8s/media-volume`'s own `pvc.yaml.j2` is `ReadWriteMany`. It does not
    corrupt this predicate today — `media-volume` itself renders no Recreate Deployment, so
    `_migrating_state` never reaches that claim — but a future Recreate role sharing that RWX
    volume would be flagged here as if it needed snapshot protection for a migration risk RWX
    doesn't actually carry the same way RWO does.
    """
    return _deployment_strategy_is_recreate(role) and bool(
        _rendered_pvc_claims(role)[0]
    )


# ── state coupled OUTSIDE the volume (2026-08-22 review M2) ─────────────────────────────────
# The exclusion class every other guard in this file misses. The checks above ask whether a role
# protects the claims it OWNS; this one asks whether it mounts a claim it does not own and
# therefore cannot revert.
#
# `_rendered_pvc_claims` reads only what a role CAUSES to exist — a PVC document in its own
# templates, or a `k8s/seed-volume` include. `media-data` is rendered by `k8s/media-volume`, so
# every *arr role mounting it is invisible to that reader. tdarr's promotion was caught by a
# human audit on 2026-08-22, not by a test; sonarr/radarr/bazarr/jellyfin were weighed and kept.
# The gap this closes is the NEXT role added with such a mount, promoted with nobody asked.
#
# The ack key is a list of claim names, not a prose reason — mechanically diffable, and the same
# shape as k8s_autodeploy_snapshot_pvcs. It proves the question was asked, never that the answer
# was right; that is the honest limit of a guard here, and it converts a silent omission into a
# visible one, which is what tdarr needed.
_CLAIM_REF_RE = re.compile(r"^\s*claimName:\s*(?P<token>\S.*?)\s*$", re.MULTILINE)


def _claim_name_refs(role: Path) -> tuple[set[str], list[str]]:
    """Claim names `role`'s own workload templates MOUNT, as `(resolved, unresolved_tokens)`.

    Deliberately distinct from `_rendered_pvc_claims`, which reads what a role creates. A role
    can mount a claim another role renders, and that is exactly the coupling being detected.

    An unresolvable token is returned rather than dropped, and the caller treats it as a
    violation — pihole's `claimName: {{ inst.claim }}` is a loop variable no single-var resolver
    can reach, and a role like that must not slip through as "no refs found". It is denied today,
    so this does not bite; it must stay a violation if one is ever promoted.
    """
    defaults = _role_defaults(role)
    tdir = role / "templates"
    resolved: set[str] = set()
    unresolved: list[str] = []
    for template in sorted(tdir.glob("*.j2")) if tdir.is_dir() else []:
        for match in _CLAIM_REF_RE.finditer(template.read_text()):
            name = _resolve_claim_token(match.group("token"), defaults)
            if name is not None:
                resolved.add(name)
            else:
                unresolved.append(f"{template.name}: {match.group('token')}")
    return resolved, unresolved
