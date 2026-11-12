"""What makes a k8s role eligible for Renovate's automerge, derived from the role sources.

The logic is what the guards agree on. A change here moves every one of them at once. The
tree-wide guards and the synthetic-role unit tests must read a role the same way, or the unit
tests stop standing for anything.

The derivation is layered, base first, and each consumer imports from the layer it needs:

  * _autodeploy.py — the roles, the denylist, the template readers, and the two declarations `_auto_deployable` and `_declares_autodeploy`
  * _autodeploy_batch.py — is a Job or CronJob credited as gated
    (test_k8s_autodeploy_batch_gates.py)
  * _autodeploy_rollout.py — is a Deployment or DaemonSet credited as gated
    (test_k8s_autodeploy_rollout_gates.py)
  * _autodeploy_claims.py — PVC and claim accounting (test_k8s_autodeploy_guard.py, which
    also holds the whole-tree assertions)
"""

import re
from pathlib import Path

from lib import yaml_fast
from lib.k8s_roles import is_leftover_dir, k8s_autodeploy_denylist
from _helpers import REPO
from _k8s_render import rendered_texts

_REPO = REPO
_K8S_ROLES = _REPO / "ansible/roles/k8s"
# Not a workload role — the shared include every other role calls. The invariant: no role in
# _SHARED may pin an `_image:` var, because that's what makes a role Renovate-visible and
# therefore auto-deployable in the first place. Both here have no defaults/main.yml at all, so
# neither pins one — that's the supporting fact, not the rule.
_SHARED = {"manifests"}

# Shared roles other roles rely on to block until a batch workload is terminal. Membership
# exempts nobody from the batch guard below; the set exists so
# `test_gating_shared_roles_actually_wait` has something to check, and it proves each role
# named here really does hold a completion gate backed by a failure escalation.
#
# What belongs here: any role under roles/k8s/ whose job is to make a caller's batch workload
# observable at deploy time. image-builder applies and waits on a build Job of its own.
_GATING_SHARED_ROLES = {"image-builder"}


def _denylist() -> set[str]:
    """The denylist as `k8s_autodeploy_counts.py --denylist` prints it."""
    return set(k8s_autodeploy_denylist(str(_REPO / "ansible")))


def _roles() -> list[Path]:
    """Every deployable role directory under roles/k8s/, shared roles excluded.

    `is_leftover_dir` skips `__pycache__`-only debris from a removed role. Without it these
    guards fail naming a "role" that no longer exists in git.
    """
    return sorted(
        p
        for p in _K8S_ROLES.iterdir()
        if p.is_dir() and p.name not in _SHARED and not is_leftover_dir(str(p))
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
    """Every file under `roles/k8s/`, the tree whose templates issue kubectl against a kind.

    Not repo-wide (READMEs and CI workflows don't run kubectl), and not `_roles()`: the sweep
    reads every role, the shared ones included.
    """
    return [
        p
        for p in sorted(_K8S_ROLES.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    ]


_RENDERED: dict[str, dict[str, str]] | None = None


def _rendered_roles() -> dict[str, dict[str, str]]:
    """Every role `_k8s_render` renders, as {role: {template name: rendered text}}."""
    global _RENDERED
    if _RENDERED is None:
        _RENDERED = {}
        for name, template, text in rendered_texts():
            _RENDERED.setdefault(name, {})[template] = text
    return _RENDERED


def _template_texts(role: Path) -> dict[str, str]:
    """`role`'s templates by name, as the deploy renders them wherever a render reaches the role.

    A role under roles/k8s/ that `_k8s_render` renders reads its render, so a workload's `kind`
    or name that moves into a role default is still seen (#3178). Two kinds of role read
    source instead, because no render reaches them: a synthetic `widget_role` tree under
    `tmp_path`, which has no inventory entry, and the caller-rendered or manifest-less roles
    `_k8s_render` skips. Every one of the latter is denylisted, and
    `test_the_render_reaches_every_auto_deployable_role` holds that, so no tree-wide guard
    judges an auto-deployable role from its source.
    """
    rendered = _rendered_roles()
    if role.parent == _K8S_ROLES and role.name in rendered:
        return rendered[role.name]
    return {t.name: t.read_text() for t in sorted((role / "templates").glob("*.j2"))}


def _deployment_templates(role: Path) -> list[str]:
    """Templates rendering a `kind: Deployment` or `kind: DaemonSet`, by name.

    Both are gated the same way: a role's rollout entries name `<kind>/<name>`, and the guards
    below check that every rendered workload is named in those entries. Matching only Deployment
    would make a DaemonSet role render zero workloads and pass every shape guard ungated.

    The `kind` match tolerates optional quoting (`kind: "Deployment"`) and an optional trailing
    comment (`kind: Deployment  # web`), the same tolerance `_batch_templates` already carries
    for `Job`/`CronJob`. No live template uses either spelling. The tolerance is load-bearing
    because `_rollout_gate_offender` trusts an empty result here as proof a role renders no
    Deployment/DaemonSet at all. A quoted or commented `kind:` line this could not see would
    grant a batch-gate exemption to a role that still has an ungated rollout.
    """
    return [
        name
        for name, text in sorted(_template_texts(role).items())
        if re.search(
            r"^kind:\s*[\"']?(?:Deployment|DaemonSet)[\"']?\s*(?:#.*)?$",
            text,
            re.MULTILINE,
        )
    ]


def _batch_templates(role: Path) -> list[tuple[str, str]]:
    """Every `kind: Job` or `kind: CronJob` a template renders, as (filename, metadata.name).

    Batch workloads are gated role-locally (`wait --for=condition=complete`), not through
    `manifests_rollout`, so they need their own offender set. Matching only
    Deployment/DaemonSet would make a Job-only role render zero workloads and pass every shape
    guard ungated.

    A template can hold several `---`-separated YAML documents, and a batch-workload
    template holding two is not hypothetical:
    registry's `selftest-pull-job.yaml.j2`, deleted in its flip, rendered both
    `registry-selftest-pull` and `registry-selftest-pull-agent` in one file. Splitting on the document separator and
    matching `kind`/`name` within each document (rather than a single `findall` for `name:`
    over the whole file) keeps a Job's name paired with that Job, not with an unrelated
    container, volume, or a non-batch document sharing the file.

    The `kind` match tolerates an optional quoting (`kind: "Job"`) and an optional trailing
    comment (`kind: Job  # one-shot`) — both valid YAML that kubectl applies identically to
    the bare form. No template uses either spelling.
    """
    out: list[tuple[str, str]] = []
    for template, text in sorted(_template_texts(role).items()):
        for doc in re.split(r"^---\s*$", text, flags=re.MULTILINE):
            if not re.search(
                r"^kind:\s*[\"']?(?:Job|CronJob)[\"']?\s*(?:#.*)?$", doc, re.MULTILINE
            ):
                continue
            name = re.search(r"^\s{2}name:\s*(\S+)\s*$", doc, re.MULTILINE)
            out.append((template, name.group(1) if name else ""))
    return out


def _strip_comments(text: str) -> str:
    """`text` with YAML/shell comments removed — whole-line AND trailing, on every line.

    A comment is the argument against a thing, and a text matcher that credits it counts the
    argument as the thing itself. A commented-out wait counts as a gate, and a comment
    explaining why a role does NOT use `kubectl wait` satisfies a check for `kubectl wait`.
    Stripping whole-line comments alone leaves the trailing form open.

    `re.MULTILINE` is what makes the trailing form strip on every line rather than only after
    the last newline. An expression that legitimately contained ` #` would be truncated, which
    can only make a check stricter.
    """
    kept = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return re.sub(r"\s#.*$", "", "\n".join(kept), flags=re.MULTILINE)


_LITERAL_NAME = re.compile(r"^[\w.-]+$")


def _auto_deployable(role: Path) -> bool:
    """Whether gitops_deploy may auto-deploy this role, per the role's own declaration.

    Fail-closed: a role that declares nothing is not auto-deployable. The completeness guard
    makes that unreachable for a role that declares, and it is still the right default for a
    role that doesn't.

    Reads defaults/main.yml as a plain FILE via yaml.safe_load, not as a live Ansible variable.
    Whoever re-points gitops_deploy at these declarations must do the same: k8s_autodeploy
    and k8s_autodeploy_reason are unprefixed keys, shared by name across every role in one play, so
    Ansible variable lookup would resolve whichever role's defaults last set them in load order —
    not the role being asked about.
    """
    defaults = role / "defaults/main.yml"
    if not defaults.is_file():
        return False
    data = yaml_fast.safe_load(defaults.read_text()) or {}
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
    data = yaml_fast.safe_load(defaults.read_text()) or {}
    return "k8s_autodeploy" in data and bool(
        str(data.get("k8s_autodeploy_reason", "")).strip()
    )


# ── k8s_autodeploy_snapshot_pvcs ─────────────────────────────────────────────────────────────
#
# The renderer writes a pre-apply Longhorn snapshot Job for a role's
# `k8s_autodeploy_snapshot_pvcs` claims. A typo'd claim name surfaces only on a real deploy. The claim-name test below is therefore the only pre-deploy catch.
#
# Most roles declare their claims as `k8s_claims` entries in defaults/main.yml
# (`name: "{{ <role>_k8s_claim }}"`) and render no PersistentVolumeClaim template of their own.
# The roles that DO render their own PVC write `metadata.name: {{ <role>_k8s_claim }}`, a Jinja
# reference, not a literal. A test that parsed only a role's own `templates/*.j2` for literal
# claim names would find an empty set for the `k8s_claims` roles and fail their correct
# declarations. `_rendered_pvc_claims` reads both sources (a role's own PVC template AND its
# `k8s_claims` names) and resolves the single-var reference both use through the role's own
# defaults/main.yml.


def _role_defaults(role: Path) -> dict:
    """`role`'s defaults/main.yml as a plain dict, or `{}` if it has none."""
    defaults = role / "defaults/main.yml"
    if not defaults.is_file():
        return {}
    return yaml_fast.safe_load(defaults.read_text()) or {}
