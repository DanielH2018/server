"""Guards on which k8s roles gitops_deploy may auto-deploy.

`roles/k8s/manifests` waits on the primary rollout —
`{{ manifests_rollout_kind | default('deploy') }}/{{ manifests_rollout | default(manifests_service) }}` — plus every
`manifests_extra_rollouts` entry, and runs `assert_stable.yml` against each. A rendered
Deployment is gated only if its own `metadata.name` matches the primary rollout name or one of
the declared extras; a name that can't be resolved statically (a Jinja expression) counts as
ungated, the fail-closed direction. Four role shapes therefore auto-deploy without a working
gate:

  * a role rendering a `kind: Deployment` whose name isn't in that gated set: `kubectl apply -f
    <dir>/` applies it but nothing waits on it, so a bump to its image is deployed and never
    verified. A typo'd or drifted `manifests_extra_rollouts` entry falls into this the same way
    an undeclared Deployment does — matching is by name, not by count. prowlarr and freshrss
    were the original instances and now declare their extras correctly, so they are gated —
    but both still declare k8s_autodeploy: false regardless, for migrating-state reasons
    (Recreate + an RWO seed-volume PVC) that gatedness never touched. Don't read "gated" as
    "eligible";
  * a role passing `manifests_rollout: ''`, which skips the rollout wait AND the stability soak
    outright. For a role rendering a Deployment or DaemonSet that is a real defect. For a
    batch-only role — no Deployment, no DaemonSet — it is correct and unavoidable, so such a
    role is exempt from this shape's offender check only on positive proof: it renders at least
    one batch workload, and every one is credited by the batch gate below. A role rendering NO
    workload at all still counts as an offender — rendering nothing is not evidence of a gate;
  * a role whose gated Deployment(s) declare no `readinessProbe`: `rollout status` then returns
    the moment the pod reports Running, which proves only that the image exists. Checked per
    Deployment, not once per role — a probe on the primary doesn't excuse a probe-less extra;
  * a role rendering a `kind: Job` or `kind: CronJob` with no role-local completion gate after
    the apply: batch workloads are never gated through `manifests_rollout` at all, so nothing
    above even attempts to wait on them. Two gate forms count, and they cover different shapes:

      - a role-local `wait --for=condition=complete job/<name>`, for a Job the role applies
        itself. Every name the wait lists is credited;
      - an `include_role: k8s/cronjob-gate` with `cronjob_gate_name: <cronjob>`, for a CronJob.
        A CronJob runs on its schedule, so nothing executes at deploy time and no `job/<name>`
        wait can be written for it at all; the shared role creates a one-off Job from the
        CronJob and blocks on it. The CronJob named there is credited, because that is the
        `metadata.name` the caller's own template renders.

    Delegation to `k8s/image-builder` is the third shape and is NOT credited: the Job lives in
    image-builder's templates, not the caller's, so a role that only delegates renders zero
    batch templates and passes this guard vacuously on an empty loop. That is a known limit,
    not a hole a delegation marker was ever able to close. `k8s/cronjob-gate` differs precisely
    because the caller does render the workload — the CronJob is the caller's, only the gating
    Job is the shared role's.

All four are fine for a hand-deployed role — an operator is watching. None is fine for an
auto-deployed one, so a role's own k8s_autodeploy declaration must cover them. Asserting it
here means a role whose gated set drifts from what it actually renders fails the suite instead
of silently auto-deploying ungated.

These guards were belt-and-braces while `gitops_deploy_k8s_autodeploy_pilot` named a single
service; clearing the pilot on 2026-08-16 made them the only thing standing between a role shape
and an ungated auto-deploy. The probe guard was added in that same commit, after six services
turned out to match an existing exclusion class while sitting outside the denylist.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from k8s_autodeploy import k8s_autodeploy_denylist

_REPO = Path(__file__).resolve().parents[2]
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
    False,
    0,
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


def test_commented_out_wait_does_not_count_as_gated(tmp_path: Path) -> None:
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
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "# disabled: k3s kubectl -n ns wait --for=condition=complete "
        "job/widget-probe --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_a_wait_in_a_trailing_shell_comment_does_not_count_as_gated(
    tmp_path: Path,
) -> None:
    """The W1 shape on the higher-stakes path: `_batch_gated_names`, not the shared-role check.

    A `#` inside a `shell: |` block scalar is literal content to YAML, so the command text a
    live task hands over can still carry a comment — and a `# TODO: wait
    --for=condition=complete job/<name>` in it would put that name in the gated set, clearing
    the very Job it says is not yet gated. The whole-line case above cannot reach here (a
    commented-out task never parses as a task at all), so this is the shape that needed
    `_strip_comments` on this path too.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Apply the probe\n"
        "  ansible.builtin.shell: |\n"
        "    k3s kubectl -n ns apply -f /tmp/probe.yaml"
        "  # TODO: wait --for=condition=complete job/widget-probe --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_single_wait_naming_two_jobs_credits_both(tmp_path: Path) -> None:
    """One `wait --for=condition=complete` naming two Jobs must credit both names.

    Synthetic mirror of registry's real `wait ... job/registry-selftest-pull
    job/registry-selftest-pull-agent`, pinned independently so the guard's behavior on a
    multi-name wait doesn't depend on that role continuing to exist or stay denylisted.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- ansible.builtin.command:\n"
        "    cmd: >-\n"
        "      k3s kubectl -n ns wait --for=condition=complete\n"
        "      job/widget-a job/widget-b --timeout=120s\n"
    )
    assert _batch_gated_names(role) == {"widget-a", "widget-b"}


def test_a_from_cronjob_token_in_the_same_command_is_not_credited(
    tmp_path: Path,
) -> None:
    """An unrelated `job/<name>`-shaped token in the same command must not be credited.

    task-3-rulings-2.md S6: before `_WAIT_JOB_NAMES` anchored the scan to the run of tokens
    immediately after the wait flag, a `--from=cronjob/otherthing` earlier in the same
    one-liner was picked up as if the wait had named it too.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Create and wait for widget-job\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.shell:\n"
        "    cmd: >-\n"
        "      k3s kubectl create job widget-job --from=cronjob/otherthing ;\n"
        "      k3s kubectl wait --for=condition=complete job/widget-job --timeout=60s\n"
    )
    assert _batch_gated_names(role) == {"widget-job"}


def test_a_jinja_job_name_in_a_wait_is_refused_outright(tmp_path: Path) -> None:
    """A `job/<name>` token that isn't a full literal must be refused, not truncated.

    task-3-rulings-2.md S7, the live instance: image-builder's wait names
    `job/build-{{ image_builder_name }}`. Truncating at the first non-`[\\w.-]` character used
    to credit the shorter, wrong-but-plausible literal `build-`; refusing the whole token
    credits nothing instead — still fail-closed, but honest about why.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Wait for the build\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.command:\n"
        "    cmd: >-\n"
        "      k3s kubectl wait --for=condition=complete\n"
        "      job/build-{{ widget_name }} --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_argv_form_wait_is_credited(tmp_path: Path) -> None:
    """An `argv:` list form of the wait command must be credited, same as `cmd:`.

    task-3-rulings-2.md S8: `k8s/cronjob-gate` itself uses `argv:` for its own container-state
    read, with a comment recording that the `cmd:` form shipped broken once — so `argv:` is an
    established spelling in this codebase, and a guard that cannot read it would false-offend
    the next role that follows the precedent.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
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


def test_short_command_module_name_is_credited(tmp_path: Path) -> None:
    """The short `command:`/`shell:` module spellings (not just the FQCN) must be credited."""
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Wait for widget-job\n"
        "  tags: [deploy]\n"
        "  command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == {"widget-job"}


def test_a_double_spaced_wait_command_is_still_credited(tmp_path: Path) -> None:
    """Extra whitespace inside the command string must not defeat the match."""
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Wait for widget-job\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.command:\n"
        "    cmd: 'k3s kubectl wait  --for=condition=complete  job/widget-job --timeout=120s'\n"
    )
    assert _batch_gated_names(role) == {"widget-job"}


def test_batch_templates_sees_quoted_and_commented_kind(tmp_path: Path) -> None:
    """`kind: "Job"` and `kind: Job  # comment` must both be seen as batch templates.

    Both are valid YAML that kubectl applies identically to the bare `kind: Job` form. No
    live template uses either spelling today, so this is prophylactic — pinned here rather
    than left to a mutation that was only ever run by hand.
    """
    role = tmp_path / "widget"
    (role / "templates").mkdir(parents=True)
    (role / "templates" / "quoted-job.yaml.j2").write_text(
        'apiVersion: batch/v1\nkind: "Job"\nmetadata:\n  name: widget-quoted\n'
    )
    (role / "templates" / "commented-job.yaml.j2").write_text(
        "apiVersion: batch/v1\nkind: Job  # one-shot\nmetadata:\n  name: widget-commented\n"
    )
    found = dict(_batch_templates(role))
    assert found == {
        "quoted-job.yaml.j2": "widget-quoted",
        "commented-job.yaml.j2": "widget-commented",
    }


def test_cronjob_gate_delegation_credits_the_named_cronjob(tmp_path: Path) -> None:
    """An `include_role: k8s/cronjob-gate` credits `cronjob_gate_name`, verbatim.

    Verbatim is the whole point. The Job the shared role creates is `<name>-deploy-gate`, but
    what `_batch_templates` yields for the caller is the CronJob's own `metadata.name` — so
    crediting the Job's name would produce a string no rendered manifest can equal, gating
    nothing while reporting the role as gated. That is the shape a delegation marker took in
    this same slice before it was deleted; this test is what stops it coming back.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Gate the widget deploy on a one-off run\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.include_role:\n"
        "    name: k8s/cronjob-gate\n"
        "  vars:\n"
        "    cronjob_gate_name: widget\n"
    )
    assert _batch_gated_names(role) == {"widget"}


def test_cronjob_gate_vars_above_the_include_are_credited(tmp_path: Path) -> None:
    """`vars:` written above `ansible.builtin.include_role:` is the same task and must count.

    Valid YAML, and Ansible runs it identically — mapping keys are unordered. Reading forward
    from the include's own `name:` line saw nothing after it and called the role ungated, which
    would tell a maintainer to add an include the role already has. Scoping to the whole task
    removes the ordering assumption rather than documenting it.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Gate the widget deploy on a one-off run\n"
        "  tags: [deploy]\n"
        "  vars:\n"
        "    cronjob_gate_name: widget\n"
        "  ansible.builtin.include_role:\n"
        "    name: k8s/cronjob-gate\n"
    )
    assert _batch_gated_names(role) == {"widget"}


def test_cronjob_gate_name_outside_the_include_is_not_credited(tmp_path: Path) -> None:
    """`cronjob_gate_name` set anywhere but inside a k8s/cronjob-gate include gates nothing.

    A set_fact, a defaults entry, or the same var handed to some other role all mention the
    name without any gate running. Anchoring the lookup to the include's own body is what
    keeps a mention from reading as a mechanism.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
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


def test_a_jinja_cronjob_gate_name_is_not_credited(tmp_path: Path) -> None:
    """A templated `cronjob_gate_name` can't be resolved here, so it counts as ungated.

    Same fail-closed choice `_deployment_name` makes for a Jinja Deployment name: guessing
    which CronJob a `{{ ... }}` resolves to is how a guard credits a workload nothing waits
    on. The role reads as an offender instead, which an operator can then answer.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Gate the widget deploy on a one-off run\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.include_role:\n"
        "    name: k8s/cronjob-gate\n"
        "  vars:\n"
        '    cronjob_gate_name: "{{ widget_cronjob }}"\n'
    )
    assert _batch_gated_names(role) == set()


def test_a_wait_gated_by_when_false_does_not_count(tmp_path: Path) -> None:
    """A `wait --for=condition=complete` task that never runs must not credit its Job.

    task-3-rulings.md R1: the text-matching predecessor of `_batch_gated_names` read no
    `when:` at all, so a wait disabled with `when: false` — plausible as a temporary "skip
    this slow probe" edit — still granted the exemption while nothing actually ran.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Wait for widget-job\n"
        "  tags: [deploy]\n"
        "  when: false\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_a_wait_tagged_never_does_not_count(tmp_path: Path) -> None:
    """A `wait --for=condition=complete` task tagged `never` must not credit its Job.

    R1: `never` is Ansible's own reserved exclusion tag — it runs only when a play requests it
    by name, which a normal auto-deploy never does.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Wait for widget-job\n"
        "  tags: [never]\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_a_wait_tagged_config_does_not_count(tmp_path: Path) -> None:
    """A `wait --for=condition=complete` task tagged only `config` must not credit its Job.

    R1: every real gate task in this repo is `tags: [deploy]`. A `--skip-tags deploy`
    config-only deploy — a normal, documented invocation, not a hypothetical one — would skip a
    `[deploy]`-tagged apply-and-wait pair entirely; crediting a wait tagged only `[config]`
    would read the role as gated under an invocation where nothing was applied for it to wait
    on.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Wait for widget-job\n"
        "  tags: [config]\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_a_debug_message_describing_a_wait_does_not_count(tmp_path: Path) -> None:
    """A `debug` task whose message merely describes a wait must not credit its Job.

    R1, and the finding that matters most: this needs no sabotage, only plausible prose — a
    comment-shaped instruction telling the operator to run the wait by hand. A `debug` module
    runs nothing; `_task_command_text` only reads `ansible.builtin.command`/`.shell`, so this
    task contributes no command text at all regardless of its `tags:`/`when:`.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Note the manual gate\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.debug:\n"
        "    msg: >-\n"
        "      run kubectl wait --for=condition=complete job/widget-job yourself\n"
    )
    assert _batch_gated_names(role) == set()


def test_a_wait_inside_a_when_false_block_does_not_count(tmp_path: Path) -> None:
    """A wait task must not credit its Job when the FALSEY `when:` sits on the enclosing block.

    task-3-rulings-2.md S1: a raw top-level walk finds the nested task but does not carry the
    block's own `when:`/`tags:` down to it, so all four R1 constructions credit again the
    moment they sit on the `block:` instead of the task. This pins the `when: false` case.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Gate group\n"
        "  when: false\n"
        "  block:\n"
        "    - name: Wait for widget-job\n"
        "      tags: [deploy]\n"
        "      ansible.builtin.command:\n"
        "        cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_a_wait_inside_a_never_tagged_block_does_not_count(tmp_path: Path) -> None:
    """S1: the `tags: [never]` case, with the tag on the enclosing block."""
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Gate group\n"
        "  tags: [never]\n"
        "  block:\n"
        "    - name: Wait for widget-job\n"
        "      ansible.builtin.command:\n"
        "        cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_a_wait_inside_a_config_tagged_block_does_not_count(tmp_path: Path) -> None:
    """S1: the `tags: [config]` case, with the tag on the enclosing block rather than the task.

    The inner task carries no tags of its own at all — its effective tags come entirely from
    the block, which is exactly the shape a bare per-task tag check misses.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Gate group\n"
        "  tags: [config]\n"
        "  block:\n"
        "    - name: Wait for widget-job\n"
        "      ansible.builtin.command:\n"
        "        cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_a_nested_block_when_false_propagates_two_levels_down(tmp_path: Path) -> None:
    """S1: a block inside a block must still propagate a falsey `when:` to the innermost task.

    `_iter_task_dicts` accumulates as it descends rather than reading only the immediate
    parent, so this is the case that would catch a merge that stopped one level too soon.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
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


def test_when_string_false_does_not_count(tmp_path: Path) -> None:
    """task-3-rulings-2.md S2: `when: "false"` (a string) skips the task exactly like the
    literal boolean, and must not credit its Job."""
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Wait for widget-job\n"
        "  tags: [deploy]\n"
        '  when: "false"\n'
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_when_no_does_not_count(tmp_path: Path) -> None:
    """S2: `when: 'no'` — a YAML-boolean-looking string Ansible also treats as falsey."""
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Wait for widget-job\n"
        "  tags: [deploy]\n"
        "  when: 'no'\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_when_capital_false_does_not_count(tmp_path: Path) -> None:
    """S2: `when: 'False'` (capitalized string)."""
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Wait for widget-job\n"
        "  tags: [deploy]\n"
        "  when: 'False'\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_when_integer_zero_does_not_count(tmp_path: Path) -> None:
    """S2: `when: 0` (an integer, not a boolean or string)."""
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Wait for widget-job\n"
        "  tags: [deploy]\n"
        "  when: 0\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_when_list_containing_a_falsy_string_does_not_count(tmp_path: Path) -> None:
    """S2: `when: ["false"]` — the falsy spelling inside a `when:` list, which ANDs its
    entries, so one falsy entry is enough to skip the task regardless of the others."""
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Wait for widget-job\n"
        "  tags: [deploy]\n"
        "  when:\n"
        '    - "false"\n'
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == set()


def test_when_referencing_a_variable_is_left_alone(tmp_path: Path) -> None:
    """A genuine Jinja `when:` (references a variable) is NOT treated as falsy — this file
    cannot evaluate it statically, so it is left alone rather than guessed at, and the task's
    wait is credited normally."""
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Wait for widget-job\n"
        "  tags: [deploy]\n"
        "  when: some_condition | bool\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    assert _batch_gated_names(role) == {"widget-job"}


def _gate_role(tmp_path: Path, body: str) -> Path:
    """A synthetic role whose tasks/main.yml is `body`, for the gate matchers to read.

    Written to disk and addressed by PATH, not handed to the matchers pre-parsed. The whole
    defect these fixtures pin is that a matcher read raw text where its sibling parsed YAML and
    applied the liveness rules — injecting a task list past `_live_tasks` would test the half
    that was never broken and leave the transport unexercised, which is how this branch once
    ran 13 green tests downstream of a broken one.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(body)
    return role


_POLL_BOTH_CONDITIONS = (
    "- name: Wait for the gate run\n"
    "  ansible.builtin.command:\n"
    "    cmd: k3s kubectl -n ns get job widget-deploy-gate -o jsonpath={.status.conditions}\n"
    "  register: r\n"
    "  until: >-\n"
    "    'Complete' in r.stdout or 'Failed' in r.stdout\n"
    "  retries: 30\n"
)


def test_a_poll_naming_only_complete_is_not_a_completion_gate(tmp_path: Path) -> None:
    """`_has_completion_gate` must reject the one-sided poll it exists to replace.

    A poll that waits only for `Complete` is `kubectl wait --for=condition=complete` wearing
    a different shape: with `backoffLimit: 0` a failed run settles in seconds and the poll
    then burns its whole retry budget before reporting anything. Accepting it would let
    cronjob-gate's poll be halved while `_GATING_SHARED_ROLES` stayed green — and
    `_batch_gated_names` credits every caller of that role.
    """
    assert _has_completion_gate(_gate_role(tmp_path / "a", _POLL_BOTH_CONDITIONS))
    assert not _has_completion_gate(
        _gate_role(
            tmp_path / "b",
            _POLL_BOTH_CONDITIONS.replace(" or 'Failed' in r.stdout", ""),
        )
    )
    # A comment naming both conditions is not a gate: the poll is read off the parsed task's
    # own `until:`, so a `#` line cannot reach it and neither can a second task's mention.
    assert not _has_completion_gate(
        _gate_role(
            tmp_path / "c",
            "# 'Complete' and 'Failed' are the terminal conditions\n"
            + _POLL_BOTH_CONDITIONS.replace(" or 'Failed' in r.stdout", ""),
        )
    )


def test_a_poll_on_a_module_that_observes_nothing_is_not_a_gate(tmp_path: Path) -> None:
    """`until:` is loop control on any module, so the poll half needs the same module rule.

    A `debug` retried until a string appears re-renders its own message and never reads the
    cluster. Crediting it would be the `debug`-describing-a-wait shape wearing loop control.
    """
    role = _gate_role(
        tmp_path,
        "- name: Pretend to poll\n"
        "  ansible.builtin.debug:\n"
        "    msg: waiting\n"
        "  until: >-\n"
        "    'Complete' in r.stdout or 'Failed' in r.stdout\n"
        "  retries: 30\n",
    )
    assert not _has_completion_gate(role)


def test_a_trailing_comment_arguing_against_a_wait_is_not_a_gate(
    tmp_path: Path,
) -> None:
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
    yaml_comment = _gate_role(
        tmp_path / "a",
        "- name: Reconcile  # we deliberately do not use wait --for=condition=complete\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl -n ns apply -f /tmp/x.yaml\n",
    )
    assert not _has_completion_gate(yaml_comment)

    shell_comment = _gate_role(
        tmp_path / "b",
        "- name: Reconcile\n"
        "  ansible.builtin.shell: |\n"
        "    k3s kubectl -n ns apply -f /tmp/x.yaml"
        "  # not wait --for=condition=complete: the Job can fail fast\n",
    )
    assert not _has_completion_gate(shell_comment)


def test_a_debug_describing_a_wait_is_not_a_completion_gate(tmp_path: Path) -> None:
    """Measured before the fix: an `ansible.builtin.debug` whose `msg:` names the wait
    satisfied `_has_completion_gate`, because `_task_command_text`'s module discipline was
    applied in `_batch_gated_names` and not here."""
    role = _gate_role(
        tmp_path,
        "- name: Tell the operator what to do\n"
        "  ansible.builtin.debug:\n"
        "    msg: run wait --for=condition=complete job/widget-probe yourself\n",
    )
    assert not _has_completion_gate(role)


def test_a_dead_wait_is_not_a_completion_gate(tmp_path: Path) -> None:
    """A real wait that a normal deploy never runs gates nothing.

    `_live_tasks` applies the same liveness rules `_batch_gated_names` does, so all four dead
    constructions are rejected here too — including when they sit on an enclosing `block:`.
    """
    wait = (
        "- name: Wait\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl -n ns wait --for=condition=complete job/widget --timeout=180s\n"
    )
    assert _has_completion_gate(_gate_role(tmp_path / "live", wait))
    assert not _has_completion_gate(
        _gate_role(tmp_path / "when", wait + "  when: false\n")
    )
    assert not _has_completion_gate(
        _gate_role(tmp_path / "never", wait + "  tags: [never]\n")
    )
    assert not _has_completion_gate(
        _gate_role(tmp_path / "config", wait + "  tags: [config]\n")
    )
    assert not _has_completion_gate(
        _gate_role(
            tmp_path / "block",
            "- name: Gate block\n"
            "  when: false\n"
            "  block:\n"
            "    - name: Wait\n"
            "      ansible.builtin.command:\n"
            "        cmd: k3s kubectl -n ns wait --for=condition=complete job/widget\n",
        )
    )


def test_failure_escalation_needs_a_fail_task_that_runs(tmp_path: Path) -> None:
    """W2's half: `"ansible.builtin.fail" in text` credited a comment and a dead task.

    This is the only thing standing behind `_batch_gated_names` crediting every
    `k8s/cronjob-gate` caller, so a fail-open here reaches five promoted roles.
    """
    live = _gate_role(
        tmp_path / "live",
        "- name: Fail on a bad image\n  ansible.builtin.fail:\n    msg: bad image\n",
    )
    assert _has_failure_escalation(live)

    commented = _gate_role(
        tmp_path / "comment",
        "- name: Report\n"
        "  ansible.builtin.debug:  # never ansible.builtin.fail here\n"
        "    msg: the deploy continues\n",
    )
    assert not _has_failure_escalation(commented)

    when_false = _gate_role(
        tmp_path / "when",
        "- name: Fail on a bad image\n"
        "  when: false\n"
        "  ansible.builtin.fail:\n"
        "    msg: bad image\n",
    )
    assert not _has_failure_escalation(when_false)

    tagged_never = _gate_role(
        tmp_path / "never",
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


def test_auto_deployable_reads_the_declaration_not_the_denylist() -> None:
    """The guards' input is the role's own declaration.

    Asserted directly so that when slice 1b deletes the denylist, this file needs no edit —
    and so a future reader cannot mistake the denylist for the source of truth.
    """
    for role in _roles():
        if not _declares_autodeploy(role):
            continue
        data = yaml.safe_load((role / "defaults/main.yml").read_text()) or {}
        assert _auto_deployable(role) is bool(data.get("k8s_autodeploy"))


def test_auto_deployable_roles_gate_every_deployment_they_render() -> None:
    offenders = []
    for role in _roles():
        if not _auto_deployable(role):
            continue
        ungated = _ungated_deployments(role)
        if ungated:
            offenders.append(
                f"{role.name}: {', '.join(ungated)} not in the gated set "
                f"{sorted(_gated_names(role))}"
            )
    assert not offenders, (
        "Auto-deployable role(s) with an ungated workload — declare the extras in "
        "manifests_extra_rollouts, or set k8s_autodeploy: false with a k8s_autodeploy_reason "
        "in the role's own defaults/main.yml (the denylist is derived from that "
        "declaration):\n" + "\n".join(offenders)
    )


_MANIFEST_KIND_TO_ROLLOUT_KIND = {"Deployment": "deploy", "DaemonSet": "daemonset"}


def test_auto_deployable_roles_gate_the_right_kind() -> None:
    """Name-only gating passes a role whose rollout kind is wrong.

    `_gated_names` compares names and nothing else, so a role setting
    `manifests_rollout: node-exporter` without `manifests_rollout_kind: daemonset` reports zero
    offenders while the shared role runs `rollout status deploy/node-exporter` against a
    Deployment that does not exist. kubectl fails loudly at deploy time; CI stays green, which
    is the failure this file exists to prevent.
    """
    offenders = []
    for role in _roles():
        if not _auto_deployable(role):
            continue
        primary = _primary_rollout_name(role)
        if not primary:
            continue
        declared = _primary_rollout_kind(role)
        for name in _deployment_templates(role):
            template = role / "templates" / name
            if _deployment_name(template) != primary:
                continue
            rendered = re.search(
                r"^kind:\s*(Deployment|DaemonSet)\s*$",
                template.read_text(),
                re.MULTILINE,
            )
            expected = _MANIFEST_KIND_TO_ROLLOUT_KIND[rendered.group(1)]
            if expected != declared:
                offenders.append(
                    f"{role.name}: {name} renders a {rendered.group(1)}, so the rollout gate "
                    f"needs manifests_rollout_kind: {expected}, but the role declares "
                    f"{declared!r}"
                )
    assert not offenders, (
        "Auto-deployable role(s) whose rollout gate names the wrong kind — `rollout status` "
        "would target a workload that does not exist:\n" + "\n".join(offenders)
    )


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


def test_auto_deployable_roles_declare_a_readiness_probe() -> None:
    offenders = []
    for role in _roles():
        if not _auto_deployable(role):
            continue
        missing = _deployments_missing_readiness_probe(role)
        if missing:
            offenders.append(
                f"{role.name}: {', '.join(missing)} has no readinessProbe — "
                f"`rollout status` returns on Running"
            )
    assert not offenders, (
        "Auto-deployable role(s) whose rollout gate proves nothing — give the workload(s) a "
        "readinessProbe, or set k8s_autodeploy: false with a k8s_autodeploy_reason in the "
        "role's own defaults/main.yml (the denylist is derived from that declaration):\n"
        + "\n".join(offenders)
    )


def test_readiness_probe_check_covers_every_gated_deployment(tmp_path: Path) -> None:
    """A probe on the primary Deployment doesn't excuse a probe-less gated extra.

    The old `any()` check would read this role as compliant — the primary has a probe, and
    `any()` stops looking once one template has one. Checking each template individually is
    what catches the extra.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "templates").mkdir()
    (role / "tasks" / "main.yml").write_text(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_extra_rollouts:\n"
        "      - name: widget-cache\n"
    )
    (role / "templates" / "deployment.yaml.j2").write_text(
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: widget\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "        - readinessProbe:\n"
        "            httpGet:\n"
        "              path: /\n"
    )
    (role / "templates" / "deployment-cache.yaml.j2").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: widget-cache\n"
    )
    assert _deployments_missing_readiness_probe(role) == ["deployment-cache.yaml.j2"]


def test_the_workload_matcher_sees_daemonsets(tmp_path: Path) -> None:
    """A DaemonSet-rendering role must be visible to the shape guards.

    Before this, `_deployment_templates` matched only `kind: Deployment`, so a DaemonSet role
    rendered zero workloads and passed every shape guard while being ungated — the failure
    mode the guards exist to catch, hidden by the matcher rather than absent.
    """
    role = tmp_path / "widget"
    (role / "templates").mkdir(parents=True)
    (role / "templates/daemonset.yaml.j2").write_text(
        "apiVersion: apps/v1\nkind: DaemonSet\nmetadata:\n  name: widget\n"
    )
    assert _deployment_templates(role) == ["daemonset.yaml.j2"]
    assert _deployment_name(role / "templates/daemonset.yaml.j2") == "widget"


def test_extra_rollouts_are_counted_as_gated() -> None:
    """prowlarr renders two Deployments and gates both, by name — it is not an offender.

    This pins the guard's matching model (identity, not count) against a role that actually
    has an extra: a rendered Deployment is gated only if its own resolved name equals the
    primary rollout name or appears in `manifests_extra_rollouts`. prowlarr stays on the
    denylist regardless — the migrating-state PVC/Recreate shape covered in its
    `k8s_autodeploy_reason`, unrelated to whether it gates cleanly — so this test is about the
    guard's model, not about prowlarr's eligibility.
    """
    prowlarr = _K8S_ROLES / "prowlarr"
    assert len(_deployment_templates(prowlarr)) == 2
    assert _primary_rollout_name(prowlarr) == "prowlarr"
    assert _extra_rollouts(prowlarr) == {"flaresolverr"}
    assert (
        _deployment_name(prowlarr / "templates" / "deployment-flaresolverr.yaml.j2")
        == "flaresolverr"
    )
    assert _ungated_deployment_count(prowlarr) == 0


def test_extra_rollout_naming_the_wrong_deployment_reads_as_ungated(
    tmp_path: Path,
) -> None:
    """A typo'd or drifted `manifests_extra_rollouts` name doesn't gate anything real.

    Matching by count alone (rendered - 1 - len(extras) == 0) read this as fully gated even
    though the declared extra's name matches neither rendered Deployment. Matching by identity
    catches it: the second Deployment's real name isn't in {primary, declared extra}.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "templates").mkdir()
    (role / "tasks" / "main.yml").write_text(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_extra_rollouts:\n"
        "      - name: widget-typo\n"
    )
    (role / "templates" / "deployment.yaml.j2").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: widget\n"
    )
    (role / "templates" / "deployment-cache.yaml.j2").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: widget-cache\n"
    )
    assert _extra_rollouts(role) == {"widget-typo"}
    assert _ungated_deployments(role) == ["deployment-cache.yaml.j2"]
    assert _ungated_deployment_count(role) == 1


def test_rollout_gate_credits_a_fully_gated_batch_role(tmp_path: Path) -> None:
    """A batch-only role that gates every rendered workload is not an offender.

    `manifests_rollout: ''` is correct and unavoidable here — there is no Deployment to roll —
    and the role-local `wait --for=condition=complete job/widget` is the alternative gate. This
    is the positive-proof case `_rollout_gate_offender` exists to recognise.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "templates").mkdir()
    (role / "tasks" / "main.yml").write_text(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''\n"
        "- ansible.builtin.command:\n"
        "    cmd: kubectl wait --for=condition=complete job/widget --timeout=180s\n"
    )
    (role / "templates" / "job.yaml.j2").write_text(
        "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: widget\n"
    )
    assert _rollout_gate_offender(role) is False


def test_rollout_gate_flags_a_batch_role_that_does_not_gate_its_workload(
    tmp_path: Path,
) -> None:
    """A batch-only role rendering an ungated Job is still an offender.

    Rendering a batch workload is not itself proof of a gate — the gate must actually credit
    that workload's own name, or nothing here has proven anything.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "templates").mkdir()
    (role / "tasks" / "main.yml").write_text(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''\n"
    )
    (role / "templates" / "job.yaml.j2").write_text(
        "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: widget\n"
    )
    assert _rollout_gate_offender(role) is True


def test_rollout_gate_flags_a_role_rendering_no_workloads(tmp_path: Path) -> None:
    """A role setting `manifests_rollout: ''` and rendering nothing at all stays an offender.

    Rendering no workloads means the batch loop is vacuous, which could otherwise look
    identical to "everything it renders is gated." The three-way split is deliberate: a role
    with nothing to gate has also offered no evidence the deploy did anything, so it stays
    fail-closed rather than earning the same pass a fully-gated role gets.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "templates").mkdir()
    (role / "tasks" / "main.yml").write_text(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''\n"
    )
    assert _rollout_gate_offender(role) is True


def test_rollout_gate_tolerates_a_trailing_comment_on_the_empty_rollout(
    tmp_path: Path,
) -> None:
    """`manifests_rollout: ''  # nothing to roll` must still be seen as empty.

    task-3-rulings.md R3: this repo comments nearly every var, and the triggering edit for
    this gap is exactly that house style applied to `manifests_rollout`. A role in this shape
    with no batch gate at all must still read as an offender — the comment must not make it
    invisible to the check.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "templates").mkdir()
    (role / "tasks" / "main.yml").write_text(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''  # nothing to roll\n"
    )
    assert _sets_empty_rollout(role) is True
    assert _rollout_gate_offender(role) is True


def test_primary_rollout_name_agrees_with_sets_empty_rollout_on_a_comment(
    tmp_path: Path,
) -> None:
    """`_primary_rollout_name` must also read the trailing comment as empty, not the service.

    task-3-rulings-2.md S4: R3 widened `_sets_empty_rollout` for
    `manifests_rollout: ''  # nothing to roll` and left this matcher anchored at end-of-line
    right after the closing quote, so the two disagreed about the same variable — one read
    "empty", the other fell through to `manifests_service` and returned the real service name.
    Latent while `_rollout_gate_offender`'s Deployment check happened to catch every affected
    role anyway, but two matchers disagreeing about one variable is a defect on its own.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''  # nothing to roll\n"
    )
    assert _primary_rollout_name(role) == ""


def _widget_with_a_deployment(
    tmp_path: Path, deployment_doc: str, deployment_filename: str = "deployment.yaml.j2"
) -> Path:
    """A role gating one batch workload (widget-job) while also rendering a Deployment.

    Shared by the three task-3-rulings.md R2 control cases below — they differ only in how
    the Deployment's `kind:` line is spelled, or whether it shares a file with another
    document.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "templates").mkdir()
    (role / "tasks" / "main.yml").write_text(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''\n"
        "- name: Wait for widget-job\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n"
    )
    (role / "templates" / "job.yaml.j2").write_text(
        "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: widget-job\n"
    )
    (role / "templates" / deployment_filename).write_text(deployment_doc)
    return role


def test_rollout_gate_still_flags_a_role_with_a_quoted_kind_deployment(
    tmp_path: Path,
) -> None:
    """A fully-gated batch workload does not exempt a role that also renders a Deployment.

    task-3-rulings.md R2: `_rollout_gate_offender` used to check only the batch workloads it
    could see, never whether the role also rendered a Deployment or DaemonSet. Paired with
    `_deployment_templates` being blind to `kind: "Deployment"` — valid YAML, applied by
    kubectl exactly like the bare form — a role in this shape read as a fully-gated batch-only
    role while its Deployment had no rollout wait at all.
    """
    role = _widget_with_a_deployment(
        tmp_path, 'apiVersion: apps/v1\nkind: "Deployment"\nmetadata:\n  name: widget\n'
    )
    assert _rollout_gate_offender(role) is True


def test_rollout_gate_still_flags_a_role_with_a_commented_kind_deployment(
    tmp_path: Path,
) -> None:
    """Same control as the quoted-kind case, for `kind: Deployment  # web`."""
    role = _widget_with_a_deployment(
        tmp_path,
        "apiVersion: apps/v1\nkind: Deployment  # web\nmetadata:\n  name: widget\n",
    )
    assert _rollout_gate_offender(role) is True


def test_rollout_gate_still_flags_a_role_with_a_split_document_deployment(
    tmp_path: Path,
) -> None:
    """Same control, for a Deployment sharing a `---`-split template with another document."""
    role = _widget_with_a_deployment(
        tmp_path,
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: widget-cfg\n"
        "---\n"
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: widget\n",
        deployment_filename="deployment-pair.yaml.j2",
    )
    assert _rollout_gate_offender(role) is True


def test_rollout_gate_flags_one_gated_and_one_ungated_batch_workload(
    tmp_path: Path,
) -> None:
    """A role gating one of two rendered Jobs is still an offender, not exempt.

    task-3-rulings.md R4: `_rollout_gate_offender`'s final line uses `any(...)`, which is
    correct — a role must gate EVERY batch workload it renders, not just one. Every test in
    this file up to this one renders at most one batch workload, so a bug that quietly swapped
    `any` for `all` would leave all of them green. This fixture is the one that would go red
    under that substitution.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "templates").mkdir()
    (role / "tasks" / "main.yml").write_text(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''\n"
        "- name: Wait for widget-a\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-a --timeout=120s\n"
    )
    (role / "templates" / "job-a.yaml.j2").write_text(
        "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: widget-a\n"
    )
    (role / "templates" / "job-b.yaml.j2").write_text(
        "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: widget-b\n"
    )
    assert _rollout_gate_offender(role) is True


def test_rollout_gate_does_not_falsely_accuse_a_role_gated_via_extras(
    tmp_path: Path,
) -> None:
    """A role skipping the primary rollout but gating its Deployment via extras is not an
    offender.

    task-3-rulings-2.md S5: R2's unconditional "renders a Deployment ⇒ offender" flagged this
    shape even though the Deployment IS gated — `manifests_extra_rollouts` rolls and soaks
    independently of the primary, so `manifests_rollout: ''` on the primary alone proves
    nothing here. The reviewer measured `_rollout_gate_offender: True` while
    `_ungated_deployments: []` for exactly this construction. The generalised rule checks
    `_ungated_deployments` (which already resolves gating by name, primary-or-extra) instead of
    "renders any Deployment at all", so this is no longer a false offender.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "templates").mkdir()
    (role / "tasks" / "main.yml").write_text(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''\n"
        "    manifests_extra_rollouts:\n"
        "      - name: widget-extra\n"
    )
    (role / "templates" / "deployment-extra.yaml.j2").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: widget-extra\n"
    )
    assert _ungated_deployments(role) == []
    assert _rollout_gate_offender(role) is False


def test_auto_deployable_roles_do_not_skip_the_rollout_gate() -> None:
    offenders = [
        f"{role.name}: passes manifests_rollout: '' with no gate proven — the rollout wait and "
        "stability soak are both skipped for the primary rollout (any manifests_extra_rollouts "
        "still roll and soak), and either it renders a Deployment/DaemonSet, or no batch "
        "workload, or a batch workload this role does not gate"
        for role in _roles()
        if _auto_deployable(role) and _rollout_gate_offender(role)
    ]
    assert not offenders, (
        "Auto-deployable role(s) with no rollout gate at all. For a role rendering a "
        "Deployment, that means restoring the rollout wait. For a batch-only role, gate every "
        "rendered Job with a role-local `wait --for=condition=complete job/<name>`, or every "
        "rendered CronJob with `include_role: k8s/cronjob-gate` "
        "(`cronjob_gate_name: <the CronJob's metadata.name>`) — see "
        "roles/k8s/cronjob-gate/CLAUDE.md. Or set k8s_autodeploy: false with a "
        "k8s_autodeploy_reason in the role's own defaults/main.yml (the denylist is derived "
        "from that declaration):\n" + "\n".join(offenders)
    )


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


def test_every_role_declares_its_autodeploy_stance() -> None:
    """Eligibility is declared where the justifying knowledge lives, not in a central list.

    Omission must not read as consent. This used to be scoped to roles pinning an `_image:`
    var, which left a mirror gap: a role with no defaults/main.yml at all — longhorn-ui and
    n8n-images, both live containers_list entries, both on the CSV denylist today — has no
    `_image:` var either, so it skipped the check entirely. If 1b treats an undeclared role as
    eligible, the way the CSV era treated denylist-absence as eligible, both flip from
    protected to auto-deployable with nobody reviewing it. Every role _roles() yields must
    declare, whether or not it pins an image.
    """
    missing = [role.name for role in _roles() if not _declares_autodeploy(role)]
    assert not missing, (
        "Role(s) with no k8s_autodeploy declaration. Add both keys to defaults/main.yml — "
        "k8s_autodeploy: true|false and a k8s_autodeploy_reason saying why:\n"
        + "\n".join(sorted(missing))
    )


def test_every_role_is_either_denied_or_declares_itself_deployable() -> None:
    """The two sets must partition the workload roles exactly.

    The filter raises on an undeclared role, so this asserts the partition holds across
    the live tree rather than trusting that it does.
    """
    denied = _denylist()
    all_roles = {p.name for p in _roles()}
    deployable = {p.name for p in _roles() if _auto_deployable(p)}
    assert denied | deployable == all_roles
    assert denied & deployable == set()


def test_manifests_rollout_kind_defaults_to_deploy() -> None:
    """Every existing caller omits manifests_rollout_kind, so the shared role's default is
    what keeps ~50 roles behaving exactly as before."""
    text = (_K8S_ROLES / "manifests/tasks/main.yml").read_text()
    assert re.search(
        r"manifests_rollout_kind\s*\|\s*default\(\s*['\"]deploy['\"]\s*\)", text
    ), "roles/k8s/manifests must default manifests_rollout_kind to 'deploy'"


def test_manifests_rollout_no_longer_hardcodes_the_deploy_kind() -> None:
    """The six hardcoded `deploy`/`deployment.apps` sites must all read the variable.

    Three in the primary rollout (restart command, batch-drain kind, apply-output check) and
    three more in the manifests_extra_rollouts loop, which duplicates the same pattern. The
    apply-output check is the one that matters most: it needs `daemonset.apps/` for a
    DaemonSet, a different string from the `daemonset` kubectl kind, and getting it wrong
    restarts a freshly created workload mid-creation.
    """
    text = (_K8S_ROLES / "manifests/tasks/main.yml").read_text()
    assert not re.search(r"rollout restart\s+deploy/", text), (
        "a rollout restart still hardcodes deploy/ — this matches regardless of line wrapping, "
        "and covers the manifests_extra_rollouts site as well as the primary one"
    )
    assert "'kind': 'deploy'," not in text, (
        "the batch-drain set_fact still hardcodes 'kind': 'deploy'"
    )
    assert "search('deployment.apps/'" not in text, (
        "the apply-output check still hardcodes deployment.apps/"
    )


def test_the_apply_output_ternary_maps_daemonset_to_the_daemonset_prefix() -> None:
    """The absence assertions cannot see a swapped ternary.

    `ternary('deployment.apps/', 'daemonset.apps/')` passes every other test in this file while
    making `kubectl apply`'s output never match — the `when:` then passes and a freshly created
    workload is `rollout restart`ed mid-creation, which is the race the condition prevents.
    """
    text = (_K8S_ROLES / "manifests/tasks/main.yml").read_text()
    swapped = "ternary('deployment.apps/', 'daemonset.apps/')"
    correct = "ternary('daemonset.apps/', 'deployment.apps/')"
    assert swapped not in text, (
        "the apply-output ternary is swapped: a daemonset would search for deployment.apps/"
    )
    assert text.count(correct) == 2, (
        f"expected the correct ternary at both the primary and extras sites, found "
        f"{text.count(correct)}"
    )


def test_manifests_rollout_kind_is_constrained_to_known_values() -> None:
    """kubectl accepts 'ds' and 'DaemonSet'; three consumers here match only 'daemonset'.

    Without this assert an alias gives a green deploy whose stabilisation gate read a
    Deployment's jsonpath off a DaemonSet and compared 0 == 0 — passing vacuously.
    """
    text = (_K8S_ROLES / "manifests/tasks/main.yml").read_text()
    assert (
        "manifests_rollout_kind | default('deploy') in ['deploy', 'daemonset']" in text
    ), (
        "roles/k8s/manifests must assert manifests_rollout_kind against the two values its "
        "consumers understand"
    )


def test_daemonset_alias_matcher_flags_kubectl_args_but_not_manifest_kind_fields() -> (
    None
):
    """Pins the boundary the sweep below depends on, so the next reader sees it rather than
    having to re-derive it from the regex.

    `kind: DaemonSet` in a manifest is correct and required by the Kubernetes API — a naive
    case-insensitive sweep would flag every daemonset.yaml.j2 in the repo. The distinction is
    kubectl argument vs. manifest field: a manifest's `kind:` line never follows a kubectl verb
    or takes the `<kind>/<name>` shorthand, so it is never matched here regardless of casing.
    """
    must_flag = [
        "kubectl get DaemonSet foo",
        "rollout status DaemonSet/foo",
        "kubectl get ds foo",
        "kubectl get daemonsets foo",
    ]
    for line in must_flag:
        assert _daemonset_alias_matches(line), f"should have flagged: {line!r}"

    must_not_flag = [
        "kind: DaemonSet",
        "  kind: DaemonSet\n",
        "kubectl get daemonset foo",
        "rollout status daemonset/foo",
    ]
    for line in must_not_flag:
        assert not _daemonset_alias_matches(line), f"should not have flagged: {line!r}"


def test_no_kubectl_invocation_spells_the_daemonset_kind_by_alias() -> None:
    """One spelling of the kind, across every file that actually issues a kubectl command
    against it — manifests/ and rollout-drain/ (both excluded from _roles()/_SHARED on
    purpose, see _kubectl_consumer_paths), every other role under roles/k8s/, and the
    post_tasks/ and tasks/ playbooks that consume a queued `kind`.

    `manifests_rollout_kind` and two other consumers match the literal 'daemonset', so a
    kubectl invocation using 'ds', 'daemonsets', or any casing of 'DaemonSet' is a working
    command that reads as a sanctioned spelling — and copying it into a parameterized role
    gives a green deploy with the stabilisation gate reading a Deployment's jsonpath off a
    DaemonSet.
    """
    offenders = []
    for path in _kubectl_consumer_paths():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for token in _daemonset_alias_matches(line):
                offenders.append(
                    f"{path.relative_to(_REPO)}:{n}: {token!r} in {line.strip()}"
                )
    assert not offenders, (
        "spell the DaemonSet kind 'daemonset', not a kubectl alias like 'ds', 'daemonsets', "
        "or 'DaemonSet' — found:\n" + "\n".join(offenders)
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


def test_a_commented_out_seed_volume_include_does_not_credit_a_claim(
    tmp_path: Path,
) -> None:
    """A `k8s/seed-volume` include disabled by a `#` must not credit its claim.

    The same trap this file's other matchers are written against, in a new shape: a text
    matcher would see `seed_volume_claim: "{{ widget_k8s_claim }}"` inside the comment block
    and credit it. Parsing through `_live_tasks` closes it — a commented-out task never parses
    as a task at all.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "defaults").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "# - name: Seed the widget volume\n"
        "#   ansible.builtin.include_role:\n"
        "#     name: k8s/seed-volume\n"
        "#   vars:\n"
        '#     seed_volume_claim: "{{ widget_k8s_claim }}"\n'
    )
    (role / "defaults" / "main.yml").write_text("widget_k8s_claim: widget-config\n")
    resolved, unresolved = _rendered_pvc_claims(role)
    assert resolved == set()
    assert unresolved == []


def test_an_unresolvable_claim_var_is_reported_not_dropped(tmp_path: Path) -> None:
    """A `{{ var }}` absent from the role's own defaults must be named, not silently dropped.

    Dropping it would let a role whose claim var was renamed or removed pass this guard on an
    empty rendered set matching an empty declared set — quietly correct only because both sides
    went blind the same way.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "defaults").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Seed the widget volume\n"
        "  ansible.builtin.include_role:\n"
        "    name: k8s/seed-volume\n"
        "  vars:\n"
        '    seed_volume_claim: "{{ widget_missing_claim }}"\n'
    )
    (role / "defaults" / "main.yml").write_text("widget_k8s_claim: widget-config\n")
    resolved, unresolved = _rendered_pvc_claims(role)
    assert resolved == set()
    assert unresolved == ["{{ widget_missing_claim }}"]


def test_pvc_template_claim_is_resolved_through_defaults(tmp_path: Path) -> None:
    """A role's own `kind: PersistentVolumeClaim` template, live-shaped: `metadata.name` is a
    single-var Jinja reference (zigbee2mqtt's and code-server's actual shape), resolved through
    the role's own defaults rather than left as the literal `{{ ... }}` string."""
    role = tmp_path / "widget"
    (role / "templates").mkdir(parents=True)
    (role / "defaults").mkdir(parents=True)
    (role / "templates" / "pvc.yaml.j2").write_text(
        "---\n"
        "apiVersion: v1\n"
        "kind: PersistentVolumeClaim\n"
        "metadata:\n"
        "  name: {{ widget_k8s_claim }}\n"
        "  namespace: homelab\n"
    )
    (role / "defaults" / "main.yml").write_text("widget_k8s_claim: widget-config\n")
    resolved, unresolved = _rendered_pvc_claims(role)
    assert resolved == {"widget-config"}
    assert unresolved == []


def test_pvc_template_claim_is_found_when_name_is_not_the_first_metadata_key(
    tmp_path: Path,
) -> None:
    """R6: `_PVC_NAME` used to require `name:` on the line immediately after `metadata:`, so a
    PVC whose metadata carried `labels:` first yielded no claim and no complaint — silently, a
    declared `k8s_autodeploy_snapshot_pvcs` entry would fail `test_snapshot_pvc_declarations_
    match_rendered_claims` for a role that was correct."""
    role = tmp_path / "widget"
    (role / "templates").mkdir(parents=True)
    (role / "defaults").mkdir(parents=True)
    (role / "templates" / "pvc.yaml.j2").write_text(
        "---\n"
        "apiVersion: v1\n"
        "kind: PersistentVolumeClaim\n"
        "metadata:\n"
        "  labels:\n"
        "    app.kubernetes.io/name: widget\n"
        "  name: {{ widget_k8s_claim }}\n"
        "  namespace: homelab\n"
    )
    (role / "defaults" / "main.yml").write_text("widget_k8s_claim: widget-config\n")
    resolved, unresolved = _rendered_pvc_claims(role)
    assert resolved == {"widget-config"}
    assert unresolved == []


def test_snapshot_pvc_declarations_match_rendered_claims() -> None:
    """A declared `k8s_autodeploy_snapshot_pvcs` entry must be a claim the role actually
    renders. A typo'd claim name snapshots nothing and fails silently at deploy time — the
    volume simply isn't protected, and nothing says so; this is the only pre-deploy catch for
    that, since --dry-run never reaches volume-snapshot (see the section comment above).

    Exercised by all thirteen live declarations — see `_rendered_pvc_claims` for why this had
    to read two sources rather than the brief's single one.
    """
    offenders = []
    for role in _roles():
        declared = _role_defaults(role).get("k8s_autodeploy_snapshot_pvcs") or []
        if not declared:
            continue
        rendered, unresolved = _rendered_pvc_claims(role)
        if unresolved:
            offenders.append(
                f"{role.name}: could not resolve claim token(s) {unresolved!r} against its "
                f"own defaults/main.yml"
            )
        missing = [c for c in declared if c not in rendered]
        if missing:
            offenders.append(
                f"{role.name}: k8s_autodeploy_snapshot_pvcs declares {missing} but the role "
                f"only renders {sorted(rendered)!r}"
            )
    assert not offenders, (
        "a declared k8s_autodeploy_snapshot_pvcs entry must be a claim the role actually "
        "renders:\n" + "\n".join(offenders)
    )


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
    role is auto-deployable — and `test_auto_deployable_migrating_state_roles_declare_snapshot_pvcs`
    below stays vacuous today for a narrower reason: the 31 roles this predicate flags and the 14
    `_auto_deployable` roles do not intersect at all, not because the two counts happen to match.

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


def test_auto_deployable_migrating_state_roles_declare_snapshot_pvcs() -> None:
    """An auto-deployable role with the Recreate + RWO-PVC shape must declare a non-empty
    `k8s_autodeploy_snapshot_pvcs`, so the pre-apply Longhorn snapshot actually runs for it.

    DELIBERATELY VACUOUS TODAY, and that is stated here rather than left for a future reader to
    discover: `_auto_deployable` is true for 14 roles (none `strategy: Recreate`, checked
    against every one's own Deployment/DaemonSet template), `_migrating_state` is true for the
    thirteen this task declared for, and the two sets do not intersect. No migrating-state role
    is auto-deployable — that is precisely what slice 7b changes, and this guard exists so that
    change can't silently reintroduce the Task 3 gap it depends on.
    `test_snapshot_pvc_declarations_match_rendered_claims` above is the guard that actually
    bites today; this project has repeatedly shipped guards that matched nothing by accident,
    and the difference here is that the vacuity is deliberate and documented rather than
    discovered.
    """
    offenders = [
        role.name
        for role in _roles()
        if _auto_deployable(role)
        and _migrating_state(role)
        and not (_role_defaults(role).get("k8s_autodeploy_snapshot_pvcs") or [])
    ]
    assert not offenders, (
        "auto-deployable role(s) with the Recreate + RWO-PVC shape declare no "
        "k8s_autodeploy_snapshot_pvcs, so a bad image swap can migrate the volume with no "
        "pre-apply recovery point:\n" + "\n".join(offenders)
    )
