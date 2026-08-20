"""Guards on which k8s roles gitops_deploy may auto-deploy.

`roles/k8s/manifests` waits on the primary rollout —
`{{ manifests_rollout_kind | default('deploy') }}/{{ manifests_rollout | default(manifests_service) }}` — plus every
`manifests_extra_rollouts` entry, and runs `assert_stable.yml` against each. A rendered
Deployment is gated only if its own `metadata.name` matches the primary rollout name or one of
the declared extras; a name that can't be resolved statically (a Jinja expression) counts as
ungated, the fail-closed direction. Three role shapes therefore auto-deploy without a working
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
    outright;
  * a role whose gated Deployment(s) declare no `readinessProbe`: `rollout status` then returns
    the moment the pod reports Running, which proves only that the image exists. Checked per
    Deployment, not once per role — a probe on the primary doesn't excuse a probe-less extra.

All three are fine for a hand-deployed role — an operator is watching. None is fine for an
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
    """
    out = []
    for t in (
        sorted((role / "templates").glob("*.j2"))
        if (role / "templates").is_dir()
        else []
    ):
        if re.search(
            r"^kind:\s*(?:Deployment|DaemonSet)\s*$", t.read_text(), re.MULTILINE
        ):
            out.append(t.name)
    return out


def _sets_empty_rollout(role: Path) -> bool:
    tasks = role / "tasks/main.yml"
    if not tasks.is_file():
        return False
    return bool(
        re.search(
            r"""manifests_rollout:\s*(""|'')\s*$""", tasks.read_text(), re.MULTILINE
        )
    )


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
    """
    tasks = role / "tasks/main.yml"
    if not tasks.is_file():
        return ""
    text = tasks.read_text()
    rollout = re.search(
        r"""^\s*manifests_rollout:\s*(?:"([^"]*)"|'([^']*)'|(\S+))\s*$""",
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


def test_auto_deployable_roles_do_not_skip_the_rollout_gate() -> None:
    offenders = [
        f"{role.name}: passes manifests_rollout: '' — rollout wait and stability soak both skipped"
        for role in _roles()
        if _auto_deployable(role) and _sets_empty_rollout(role)
    ]
    assert not offenders, (
        "Auto-deployable role(s) with no rollout gate at all — set k8s_autodeploy: false "
        "with a k8s_autodeploy_reason in the role's own defaults/main.yml (the denylist is "
        "derived from that declaration):\n" + "\n".join(offenders)
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
