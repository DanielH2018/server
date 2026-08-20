"""Guards on which k8s roles gitops_deploy may auto-deploy.

`roles/k8s/manifests` waits on the primary rollout —
`deploy/{{ manifests_rollout | default(manifests_service) }}` — plus every
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
    but both stay on the denylist regardless, for migrating-state reasons (Recreate + an RWO
    seed-volume PVC) that gatedness never touched. Don't read "gated" as "eligible";
  * a role passing `manifests_rollout: ''`, which skips the rollout wait AND the stability soak
    outright;
  * a role whose gated Deployment(s) declare no `readinessProbe`: `rollout status` then returns
    the moment the pod reports Running, which proves only that the image exists. Checked per
    Deployment, not once per role — a probe on the primary doesn't excuse a probe-less extra.

All three are fine for a hand-deployed role — an operator is watching. None is fine for an
auto-deployed one, so the denylist must cover them. Asserting it here means a role whose gated
set drifts from what it actually renders fails the suite instead of silently auto-deploying
ungated.

These guards were belt-and-braces while `gitops_deploy_k8s_autodeploy_pilot` named a single
service; clearing the pilot on 2026-08-16 made them the only thing standing between a role shape
and an ungated auto-deploy. The probe guard was added in that same commit, after six services
turned out to match an existing exclusion class while sitting outside the denylist.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_K8S_ROLES = _REPO / "ansible/roles/k8s"
_DEPLOYER_DEFAULTS = _REPO / "ansible/roles/setup/gitops_deploy/defaults/main.yml"
# Not a workload role — the shared include every other role calls.
_SHARED = {"manifests", "seed-volume", "rollout-drain"}


def _denylist() -> set[str]:
    data = yaml.safe_load(_DEPLOYER_DEFAULTS.read_text())
    return set(data["gitops_deploy_k8s_autodeploy_denylist"])


def _roles() -> list[Path]:
    return sorted(
        p for p in _K8S_ROLES.iterdir() if p.is_dir() and p.name not in _SHARED
    )


def _deployment_templates(role: Path) -> list[str]:
    """Templates rendering a `kind: Deployment`, by name."""
    out = []
    for t in (
        sorted((role / "templates").glob("*.j2"))
        if (role / "templates").is_dir()
        else []
    ):
        if re.search(r"^kind:\s*Deployment\s*$", t.read_text(), re.MULTILINE):
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


_DEPLOYMENT_NAME = re.compile(
    r"^kind:\s*Deployment\s*$\n\s*metadata:\s*$\n\s*name:\s*(.+?)\s*$", re.MULTILINE
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


def test_auto_deployable_roles_gate_every_deployment_they_render() -> None:
    denylist = _denylist()
    offenders = []
    for role in _roles():
        if role.name in denylist:
            continue
        ungated = _ungated_deployments(role)
        if ungated:
            offenders.append(
                f"{role.name}: {', '.join(ungated)} not in the gated set "
                f"{sorted(_gated_names(role))}"
            )
    assert not offenders, (
        "Auto-deployable role(s) with an ungated Deployment — declare the extras in "
        "manifests_extra_rollouts, or add the role to gitops_deploy_k8s_autodeploy_denylist "
        "with a reason:\n" + "\n".join(offenders)
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
    denylist = _denylist()
    offenders = []
    for role in _roles():
        if role.name in denylist:
            continue
        missing = _deployments_missing_readiness_probe(role)
        if missing:
            offenders.append(
                f"{role.name}: {', '.join(missing)} has no readinessProbe — "
                f"`rollout status` returns on Running"
            )
    assert not offenders, (
        "Auto-deployable role(s) whose rollout gate proves nothing — add to "
        "gitops_deploy_k8s_autodeploy_denylist with a reason, or give the Deployment(s) a "
        "readinessProbe:\n" + "\n".join(offenders)
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
    denylist = _denylist()
    offenders = [
        f"{role.name}: passes manifests_rollout: '' — rollout wait and stability soak both skipped"
        for role in _roles()
        if role.name not in denylist and _sets_empty_rollout(role)
    ]
    assert not offenders, (
        "Auto-deployable role(s) with no rollout gate at all — add to "
        "gitops_deploy_k8s_autodeploy_denylist with a reason:\n" + "\n".join(offenders)
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


def test_declarations_match_the_denylist_they_will_replace() -> None:
    """The declaration set and the CSV denylist must agree before the deployer switches.

    Slice 1b re-points gitops_deploy at the declarations. If the two disagree at that moment,
    the switch silently changes which services auto-deploy — so pin the equivalence here,
    while the denylist is still the live input and a mismatch is harmless.

    Scoped by _declares_autodeploy(), not _pins_an_image(): a role with no defaults/main.yml
    at all (longhorn-ui, n8n-images before round 1) has no `_image:` var either, so filtering
    on _pins_an_image() skipped the exact roles that need checking most. The completeness test
    above already fails on a role with no declaration, so this one only needs to check the
    declarations that exist.
    """
    denylist = _denylist()
    mismatched = []
    for role in _roles():
        if not _declares_autodeploy(role):
            continue
        data = yaml.safe_load((role / "defaults/main.yml").read_text()) or {}
        declared = bool(data.get("k8s_autodeploy"))
        if declared == (role.name in denylist):
            mismatched.append(
                f"{role.name}: declares k8s_autodeploy={declared} but "
                f"{'is' if role.name in denylist else 'is not'} denylisted"
            )
    assert not mismatched, "\n".join(mismatched)
