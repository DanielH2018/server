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
    (Recreate + an RWO volume-claim PVC) that gatedness never touched. Don't read "gated" as
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

Split note: the synthetic-role unit tests for the two gate kinds moved to
test_k8s_autodeploy_batch_gates.py and test_k8s_autodeploy_rollout_gates.py, and the derivation
they share is _autodeploy.py. What stays here is what reads the live tree — the declaration
partition, the roles/k8s/manifests contract, and the PVC/claim accounting.
"""

from __future__ import annotations

import re
from pathlib import Path

from _autodeploy import (
    _K8S_ROLES,
    _REPO,
    _auto_deployable,
    _daemonset_alias_matches,
    _declares_autodeploy,
    _denylist,
    _kubectl_consumer_paths,
    _role_defaults,
    _roles,
)
from _autodeploy_claims import (
    _claim_name_refs,
    _migrating_state,
    _rendered_pvc_claims,
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


def test_a_commented_out_seed_volume_include_does_not_credit_a_claim(
    tmp_path: Path,
) -> None:
    """A `k8s/volume-claim` include disabled by a `#` must not credit its claim.

    The same trap this file's other matchers are written against, in a new shape: a text
    matcher would see `volume_claim_name: "{{ widget_k8s_claim }}"` inside the comment block
    and credit it. Parsing through `_live_tasks` closes it — a commented-out task never parses
    as a task at all.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "defaults").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "# - name: Seed the widget volume\n"
        "#   ansible.builtin.include_role:\n"
        "#     name: k8s/volume-claim\n"
        "#   vars:\n"
        '#     volume_claim_name: "{{ widget_k8s_claim }}"\n'
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
        "    name: k8s/volume-claim\n"
        "  vars:\n"
        '    volume_claim_name: "{{ widget_missing_claim }}"\n'
    )
    (role / "defaults" / "main.yml").write_text("widget_k8s_claim: widget-config\n")
    resolved, unresolved = _rendered_pvc_claims(role)
    assert resolved == set()
    assert unresolved == ["{{ widget_missing_claim }}"]


def test_pvc_template_claim_is_resolved_through_defaults(tmp_path: Path) -> None:
    """A role's own PersistentVolumeClaim template, live-shaped and resolved through its defaults.

    `metadata.name` is a single-var Jinja reference (zigbee2mqtt's and code-server's actual shape),
    resolved through the role's own defaults rather than left as the literal `{{ ... }}` string.
    """
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
    """R6: a PVC whose metadata carries `labels:` first must still yield a claim.

    `_PVC_NAME` used to require `name:` on the line immediately after `metadata:`, so a PVC whose
    metadata carried `labels:` first yielded no claim and no complaint — silently, a declared
    `k8s_autodeploy_snapshot_pvcs` entry would fail `test_snapshot_pvc_declarations_
    match_rendered_claims` for a role that was correct.
    """
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
    """A declared `k8s_autodeploy_snapshot_pvcs` entry must be a claim the role actually renders.

    A typo'd claim name snapshots nothing and fails silently at deploy time — the volume simply
    isn't protected, and nothing says so; this is the only pre-deploy catch for that, since
    --dry-run never reaches volume-snapshot (see the section comment above).

    Exercised by all thirteen live declarations — see `_rendered_pvc_claims` for why this had to
    read two sources rather than the brief's single one.
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


def test_auto_deployable_migrating_state_roles_declare_snapshot_pvcs() -> None:
    """An auto-deployable role with the Recreate + RWO-PVC shape must declare a non-empty
    `k8s_autodeploy_snapshot_pvcs`, so the pre-apply Longhorn snapshot actually runs for it.

    WAS DELIBERATELY VACUOUS through slice 7a, and 7a's ledger carried that forward rather
    than leaving a future reader to discover it: `_auto_deployable` was true for 14 roles (none
    `strategy: Recreate`), `_migrating_state` was true for the thirteen slice 7a task 3 declared
    `k8s_autodeploy_snapshot_pvcs` for, and the two sets did not intersect. Slice 7b task 7
    promoted twelve of those thirteen; four of the thirteen now stay denylisted — `code-server`
    for an unrelated reason (its image is an immutable `registry/…:latest` ref with no version
    signal to auto-deploy on), and `zigbee2mqtt`/`livesync`/`qbittorrent` because the same-day
    scope decision found their volume's state coupled to something outside it (coordinator
    NVRAM, connected Obsidian clients, the referenced data volume) that a revert can
    desynchronise — a different question from whether the snapshot covers the volume, which it
    does for all three — and tdarr, re-denied by a later audit for the same shared-media
    coupling. So this guard runs against a non-empty offender set of eight and asserts
    it stays empty: every promoted role already declares its snapshot claims, so the assertion
    passes on real coverage rather than on nothing to check.
    `test_snapshot_pvc_declarations_match_rendered_claims` above is the guard that actually bit
    before this; this project has repeatedly shipped guards that matched nothing by accident,
    and the difference here was that the vacuity used to be deliberate and documented rather
    than discovered.
    """
    candidates = [
        role.name
        for role in _roles()
        if _auto_deployable(role) and _migrating_state(role)
    ]
    assert candidates, (
        "no auto-deployable role has the Recreate + RWO-PVC shape — this guard has gone back "
        "to vacuous. Reverting four of slice 7b's twelve promotions for state coupling "
        "(zigbee2mqtt, livesync, qbittorrent, then tdarr) left eight, so this staying non-empty "
        "is expected; "
        "only a full revert of all twelve should relax this back to documenting vacuity. If it's "
        "empty for any other reason, _auto_deployable or _migrating_state has drifted from what "
        "the roles actually declare."
    )
    offenders = [
        role
        for role in candidates
        if not (
            _role_defaults(_K8S_ROLES / role).get("k8s_autodeploy_snapshot_pvcs") or []
        )
    ]
    assert not offenders, (
        "auto-deployable role(s) with the Recreate + RWO-PVC shape declare no "
        "k8s_autodeploy_snapshot_pvcs, so a bad image swap can migrate the volume with no "
        "pre-apply recovery point:\n" + "\n".join(offenders)
    )


def test_auto_deployable_roles_account_for_every_claim_they_mount() -> None:
    """Every claim an auto-deployable role mounts is either revert-covered or explicitly acked.

    Reverting the fix — dropping `k8s_autodeploy_unreverted_claims` from a role that mounts
    `media-data` — fails this test by name.
    """
    offenders = []
    for role in sorted(_K8S_ROLES.iterdir()):
        if not role.is_dir():
            continue
        defaults = _role_defaults(role)
        if not defaults.get("k8s_autodeploy"):
            continue

        mounted, unresolved = _claim_name_refs(role)
        owned, _ = _rendered_pvc_claims(role)
        snapshotted = set(defaults.get("k8s_autodeploy_snapshot_pvcs") or [])
        acked = set(defaults.get("k8s_autodeploy_unreverted_claims") or [])

        foreign = mounted - owned - snapshotted - acked
        if foreign:
            offenders.append(
                f"  {role.name}: mounts {sorted(foreign)}, which it neither renders nor "
                f"declares in k8s_autodeploy_snapshot_pvcs nor acks in "
                f"k8s_autodeploy_unreverted_claims"
            )
        if unresolved:
            offenders.append(
                f"  {role.name}: claimName token(s) this reader cannot resolve "
                f"({unresolved}) — an auto-deployable role must not mount a claim whose name "
                f"nothing here can check"
            )

    assert not offenders, (
        "auto-deployable role(s) mount state that k8s/volume-revert will NOT roll back, with "
        "nobody having recorded that the trade-off was weighed. Either add the claim to "
        "k8s_autodeploy_snapshot_pvcs (if reverting it is safe) or list it in "
        "k8s_autodeploy_unreverted_claims with the reason in k8s_autodeploy_reason:\n"
        + "\n".join(offenders)
    )
