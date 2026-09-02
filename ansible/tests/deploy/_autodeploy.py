"""How gitops_deploy decides a k8s role may auto-deploy, derived from the role sources.

Split out of test_k8s_autodeploy_guard.py, which had grown to 2,712 lines holding both this
derivation and the three test groups built on it. The logic is what the guards agree on; a
change here moves every one of them at once, which is the point — the tree-wide guards and the
synthetic-role unit tests must read a role the same way or the unit tests stop standing for
anything.

The derivation is layered, base first, and each consumer imports from the layer it needs:

  * _autodeploy.py — the roles, the denylist, the task walker that decides what runs on a
    normal deploy, and the two declarations `_auto_deployable` and `_declares_autodeploy`
  * _autodeploy_batch.py — is a Job or CronJob credited as gated
    (test_k8s_autodeploy_batch_gates.py)
  * _autodeploy_rollout.py — is a Deployment or DaemonSet credited as gated
    (test_k8s_autodeploy_rollout_gates.py)
  * _autodeploy_claims.py — PVC and claim accounting (test_k8s_autodeploy_guard.py, which
    also holds the whole-tree assertions)
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
# neither pins one — that's the supporting fact, not the rule. volume-claim pins
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


_LITERAL_NAME = re.compile(r"^[\w.-]+$")


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
# creation entirely to the shared `k8s/volume-claim` role (`include_role: k8s/volume-claim`,
# `vars: volume_claim_name: "{{ <role>_k8s_claim }}"`) and render no PersistentVolumeClaim
# document of their own at all. The literal recipe would find an empty rendered set for those
# eleven and fail every one of their correct declarations — not vacuous, wrong. And the two
# roles that DO render their own PVC (zigbee2mqtt, code-server's workspace claim) still write
# `metadata.name: {{ <role>_k8s_claim }}`, a Jinja reference, not a literal — so even those two
# need the same resolution step. `_rendered_pvc_claims` below is the adapted version: it reads
# both sources (a role's own PVC template AND a `volume_claim_name` var on a live
# `k8s/volume-claim` include) and resolves the single-var-reference shape both use through the
# role's own defaults/main.yml. This is a finding about the brief, not about any role's claim
# name — every claim in the table was independently verified against `kubectl -n homelab get
# pvc` on 2026-08-21 and all fourteen are live.


def _role_defaults(role: Path) -> dict:
    """`role`'s defaults/main.yml as a plain dict, or `{}` if it has none."""
    defaults = role / "defaults/main.yml"
    if not defaults.is_file():
        return {}
    return yaml.safe_load(defaults.read_text()) or {}
