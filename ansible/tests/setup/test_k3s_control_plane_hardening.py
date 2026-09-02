#!/usr/bin/env python3
"""Guards the three control-plane hardening controls added 2026-08-20.

Each test here encodes a way one of them fails while every surface still reads green:

  * A QUOTED apiserver argument. The install task's `when:` compares
    `k3s_server_args.split()` against the installed unit with single quotes stripped,
    because the installer single-quotes each argument it writes. A quoted value never
    matches its own installed form, so the comparison reports a missing argument on every
    play and reinstalls k3s each time — restarting the control plane, which also serves LAN
    DNS, on every deploy. Nothing else would flag that; the play just looks noisy.

  * The audit policy written AFTER the installer. The apiserver reads the policy at startup
    and refuses to start without it, and the installer restarts k3s itself. Get the order
    wrong and the arming run wedges the control plane.

  * Secrets logged at a body-bearing level. `--secrets-encryption` exists to keep Secret
    values out of a plaintext copy on disk; an audit policy at `Request` or `RequestResponse`
    on Secrets writes those same values to a plaintext file on the same disk, cancelling it.

  * PSA at `enforce`. Nothing in this repo would reject the label, and PSA evaluates at pod
    create — so an enforcing label lands green and then refuses the next rollout of a
    privileged workload, hours or days later.

Run: uv run pytest ansible/tests/setup/test_k3s_control_plane_hardening.py
"""

import re

import jinja2
import yaml
from _helpers import ANSIBLE
from _helpers import load_yaml


K3S = ANSIBLE / "roles" / "setup" / "k3s"
ALL_VARS = ANSIBLE / "inventory" / "group_vars" / "all.yml"


def _defaults() -> dict:
    return load_yaml(K3S / "defaults" / "main.yml")


def _all_vars() -> dict:
    return load_yaml(ALL_VARS)


def _env() -> jinja2.Environment:
    """Ansible's Templar whitespace flags, matching scripts/lib/render_guard.py:make_env.

    Not cosmetic. With trim_blocks=True a `{%- if %}` also eats the newline BEFORE the tag,
    collapsing the next line into the previous one — which is how a Namespace template that
    renders correctly under plain Jinja produces `name: observability  # comment` under a
    real deploy. Rendering with different flags than production tests a file nobody ships.
    """
    return jinja2.Environment(
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )


def _rendered_server_args(**overrides) -> str:
    """k3s_server_args with its Jinja resolved, as the installer would receive it.

    The context merges group_vars OVER role defaults, in that precedence, because the audit
    variables deliberately live in group_vars — setup/k3s and loki-homelab both read them.
    Building it from role defaults alone raises on the first undefined instead.
    """
    defaults = _defaults()
    context = {
        "server_ip": "10.0.0.215",
        **{k: v for k, v in defaults.items() if k != "k3s_server_args"},
        **_all_vars(),
        **overrides,
    }
    # Two passes: the argument string interpolates k3s_audit_log_path, which is itself a
    # template referencing k3s_audit_log_dir. Only the variables this argument string reads
    # are resolved — group_vars is full of values using Ansible-only filters (`bool`,
    # `sops_decrypt`) that plain Jinja cannot compile, and none of them appears here.
    env = _env()
    for key in ("k3s_audit_log_path", "k3s_audit_log_dir"):
        value = context.get(key)
        if isinstance(value, str) and "{{" in value:
            context[key] = env.from_string(value).render(**context)
    return env.from_string(defaults["k3s_server_args"]).render(**context)


def _server_tasks() -> list[dict]:
    return yaml.safe_load((K3S / "tasks" / "server.yml").read_text()) or []


def _task_index(predicate) -> int:
    for i, task in enumerate(_server_tasks()):
        if predicate(task):
            return i
    return -1


def _audit_policy() -> dict:
    """The policy template rendered with the values the role passes it."""
    defaults = _defaults()
    source = (K3S / "templates" / "audit-policy.yaml.j2").read_text()
    rendered = (
        _env()
        .from_string(source)
        .render(
            k3s_readonly_sa_name=defaults["k3s_readonly_sa_name"],
            k3s_readonly_sa_namespace=defaults["k3s_readonly_sa_namespace"],
        )
    )
    return yaml.safe_load(rendered)


# ── G1: secrets encryption ──────────────────────────────────────────────────────────────


def test_secrets_encryption_is_armed():
    assert "--secrets-encryption" in _rendered_server_args(), (
        "k3s_server_args must pass --secrets-encryption. Without it every Secret is stored "
        "in etcd as plaintext, and etcd-snapshot-offbox.sh ships a daily copy of that to R2."
    )


def test_secrets_encryption_has_a_rollback_lever():
    assert (
        _rendered_server_args(k3s_secrets_encryption=False).find("--secrets-encryption")
        == -1
    ), (
        "k3s_secrets_encryption=false must drop the flag from k3s_server_args. Without a "
        "lever the only way back is hand-editing the systemd unit."
    )


def test_existing_secrets_are_reencrypted():
    """Arming the flag encrypts WRITES only; without this the old plaintext stays."""
    text = (K3S / "tasks" / "server.yml").read_text()
    assert "secrets-encrypt rotate-keys" in text, (
        "The role must run `k3s secrets-encrypt rotate-keys` after arming the flag. "
        "Encryption applies to writes, so Secrets already in etcd stay plaintext — and every "
        "check reads green, because encryption really is enabled."
    )
    assert "secrets-encrypt reencrypt" not in text, (
        "Use `rotate-keys`, not bare `reencrypt`. On k3s v1.30+ rotate-keys is the whole "
        "procedure; `reencrypt` belongs to the legacy prepare -> rotate -> reencrypt sequence "
        "and is invalid from the `start` stage a freshly armed cluster is in. It fails AFTER "
        "the control-plane restart, leaving encryption enabled over untouched plaintext."
    )
    assert "reencrypt_finished" in text, (
        "The re-encryption must be guarded on the rotation stage reaching reencrypt_finished, "
        "or it either reruns on every play (~5 Secrets/s of etcd churn) or is never verified "
        "to have completed."
    )


# ── G3: audit logging ───────────────────────────────────────────────────────────────────


def test_audit_flags_are_armed():
    args = _rendered_server_args()
    for flag in ("audit-policy-file", "audit-log-path"):
        assert f"--kube-apiserver-arg={flag}=" in args, (
            f"k3s_server_args must pass --kube-apiserver-arg={flag}=… . The apiserver keeps "
            "no audit log at all without both."
        )


def test_audit_log_rotation_is_configured():
    """An unrotated audit log fills the disk disk-health.sh watches."""
    args = _rendered_server_args()
    for flag in ("audit-log-maxage", "audit-log-maxbackup", "audit-log-maxsize"):
        assert f"--kube-apiserver-arg={flag}=" in args, (
            f"k3s_server_args must pass --kube-apiserver-arg={flag}=… . The apiserver does "
            "not rotate its audit log by default, so a Metadata-level policy on a cluster "
            "this chatty grows without bound on the control-plane disk."
        )


def test_no_apiserver_argument_is_quoted():
    """A quoted argument reinstalls k3s on every play — see the module docstring."""
    args = _rendered_server_args()
    offenders = [
        tok
        for tok in args.split()
        if tok.startswith("--") and ("'" in tok or '"' in tok)
    ]
    assert not offenders, (
        f"Quoted arguments in k3s_server_args: {offenders}. The install task strips quotes "
        "from the installed unit before comparing, so a quoted value never matches itself — "
        "k3s is reinstalled and the control plane restarted on every single play."
    )


def test_audit_policy_is_written_before_the_installer_runs():
    """The apiserver refuses to start without the policy, and the installer restarts it."""
    policy_at = _task_index(
        lambda t: (
            "audit-policy.yaml.j2"
            in str(t.get("ansible.builtin.template", {}).get("src", ""))
        )
    )
    logdir_at = _task_index(
        lambda t: (
            "k3s_audit_log_dir"
            in str(t.get("ansible.builtin.file", {}).get("path", ""))
        )
    )
    install_at = _task_index(
        lambda t: (
            "k3s-install.sh" in str(t.get("ansible.builtin.command", {}).get("cmd", ""))
        )
    )
    assert policy_at >= 0, "No task writes audit-policy.yaml.j2."
    assert logdir_at >= 0, "No task creates k3s_audit_log_dir."
    assert install_at >= 0, "No task runs k3s-install.sh — the role was restructured."
    assert policy_at < install_at and logdir_at < install_at, (
        f"The audit policy (task {policy_at}) and log directory (task {logdir_at}) must both "
        f"precede the k3s installer (task {install_at}). The installer rewrites the unit and "
        "restarts k3s, and the apiserver refuses to start when the policy file named in its "
        "arguments does not exist — on the node that also serves LAN DNS."
    )


def test_audit_policy_is_a_valid_policy_document():
    policy = _audit_policy()
    assert policy["apiVersion"] == "audit.k8s.io/v1", policy.get("apiVersion")
    assert policy["kind"] == "Policy", policy.get("kind")
    assert policy["rules"], "An audit policy with no rules logs nothing."
    assert policy["rules"][-1] == {"level": "Metadata"}, (
        "The last rule must be an unqualified catch-all. Requests matching no rule are not "
        "logged, so without it the policy silently covers only what earlier rules name."
    )


def test_secret_bodies_are_never_written_to_the_audit_log():
    """An audit log that leaks what --secrets-encryption protects is worse than none."""
    for rule in _audit_policy()["rules"]:
        if rule.get("level") not in ("Request", "RequestResponse"):
            continue
        named = {
            r
            for entry in rule.get("resources", []) or []
            for r in entry.get("resources", []) or []
        }
        assert "secrets" not in named, (
            f"Audit rule {rule} logs Secret bodies at {rule['level']}. That writes every "
            "credential in the cluster to a plaintext file on the same disk as etcd, "
            "cancelling --secrets-encryption."
        )
        assert not rule.get("resources"), (
            f"Audit rule {rule} logs bodies at {rule['level']}. Keep body-bearing levels off "
            "entirely rather than relying on a resource list to exclude Secrets — the next "
            "resource added to that list is the one nobody re-checks."
        )


def _named_resources(rule: dict) -> set[str]:
    return {
        r for entry in rule.get("resources") or [] for r in entry.get("resources") or []
    }


# The identities whose Secret reads the policy may drop ahead of the Metadata rule. Both
# entries are machine identities the policy already drops wholesale further down; widening
# this set is how the finding comes back, so it is pinned here rather than derived.
SECRET_READ_DROP_USERS = {"system:apiserver", "system:kube-proxy"}
SECRET_READ_DROP_GROUPS = {"system:nodes"}
READ_VERBS = {"get", "list", "watch"}


def test_secret_reads_are_logged_at_metadata():
    """Encryption defends the etcd snapshot; only this rule records an API-level read.

    Position is the whole test. Until 2026-08-22 the policy's first rule dropped every
    `get`/`list`/`watch` unconditionally, so a Metadata rule naming secrets was present in
    spirit and matched nothing — a `kubectl get secrets` the readonly SA was refused left no
    trace at all. Asserting only that some rule logs secrets passes with that bug live.
    """
    rules = _audit_policy()["rules"]
    at = next(
        (
            i
            for i, rule in enumerate(rules)
            if rule.get("level") == "Metadata" and "secrets" in _named_resources(rule)
        ),
        -1,
    )
    assert at >= 0, (
        "No audit rule logs Secret access at Metadata. --secrets-encryption protects the "
        "etcd snapshot and nothing else, so without this rule an API-level Secret read — "
        "the access the encryption exists to guard against — is never recorded."
    )
    verbs = set(rules[at].get("verbs") or [])
    assert not verbs or READ_VERBS <= verbs, (
        f"The Metadata Secret rule covers verbs {sorted(verbs)}. It must cover get, list "
        "and watch, or omit `verbs` entirely — a read it does not name is not logged."
    )

    for i, rule in enumerate(rules[:at]):
        if rule.get("level") not in ("None", None):
            continue
        if rule.get("nonResourceURLs"):
            continue
        rule_verbs = set(rule.get("verbs") or [])
        if rule_verbs and not rule_verbs & READ_VERBS:
            continue
        if rule.get("resources") and "secrets" not in _named_resources(rule):
            continue
        users = set(rule.get("users") or [])
        groups = set(rule.get("userGroups") or [])
        assert (
            (users or groups)
            and users <= SECRET_READ_DROP_USERS
            and groups <= SECRET_READ_DROP_GROUPS
        ), (
            f"Audit rule {i} ({rule}) drops Secret reads ahead of the Metadata rule at "
            f"{at}, and it is not scoped to the machine identities the policy drops "
            "wholesale anyway. The first matching rule wins, so this rule swallows the "
            "human and ServiceAccount Secret reads the Metadata rule exists to record."
        )


def test_promtail_tails_the_audit_log():
    """A log nobody can read is not a control — and the tail has two halves."""
    loki = ANSIBLE / "roles" / "k8s" / "loki-homelab" / "templates"
    config = (loki / "configmap.yaml.j2").read_text()
    daemonset = (loki / "promtail-daemonset.yaml.j2").read_text()
    assert "k3s_audit_log_path" in config, (
        "promtail's config must declare a scrape job for k3s_audit_log_path. Otherwise the "
        "audit log is readable only as root on daniel-box, which means in practice nobody "
        "reads it."
    )
    assert "k3s_audit_log_dir" in daemonset, (
        "promtail's DaemonSet must hostPath-mount k3s_audit_log_dir. A scrape job whose path "
        "is not mounted into the container tails nothing and reports no error — the job just "
        "quietly finds no targets."
    )


def test_audit_log_vars_are_visible_to_both_roles():
    """A role default cannot be read by another role's template."""
    shared, role = _all_vars(), _defaults()
    for name in ("k3s_audit_log_enabled", "k3s_audit_log_dir", "k3s_audit_log_path"):
        assert name in shared, (
            f"{name} must live in group_vars/all.yml. setup/k3s writes the log and "
            "loki-homelab tails it; as a role default the second role sees only the "
            "`| default(false)` fallback, so promtail silently never tails the file."
        )
        assert name not in role, (
            f"{name} must not ALSO be a k3s role default — two definitions drift, and the "
            "role default is the one that looks authoritative while losing to group_vars."
        )


# ── G2: Pod Security Admission ──────────────────────────────────────────────────────────


def test_psa_labels_warn_and_audit():
    labels = _all_vars()["k8s_psa_labels"]
    assert labels.get("pod-security.kubernetes.io/warn") == "baseline", labels
    assert labels.get("pod-security.kubernetes.io/audit") == "baseline", labels


def test_psa_does_not_enforce():
    """Enforcing today refuses seven workloads in homelab and four in observability."""
    labels = _all_vars()["k8s_psa_labels"]
    assert "pod-security.kubernetes.io/enforce" not in labels, (
        "k8s_psa_labels must not carry an `enforce` level. PSA evaluates at pod create, so "
        "the label applies green and then refuses the next rollout of nut, scrutiny, "
        "node-exporter, registry or any hostPath workload. Read the warn/audit output first, "
        "and split namespaces before enforcing anything."
    )


def test_both_namespaces_carry_the_psa_labels():
    """Labels on one namespace and not the other is the asymmetry slice 5 already produced."""
    obs = (
        ANSIBLE / "roles" / "k8s" / "claude-otel" / "templates" / "00-namespace.yaml.j2"
    ).read_text()
    assert "k8s_psa_labels" in obs, (
        "The observability Namespace template must render k8s_psa_labels, or the two "
        "namespaces diverge and only one of them reports violations."
    )
    deploy = (ANSIBLE / "deploy.yml").read_text()
    assert "k8s_psa_labels" in deploy, (
        "The workload Namespace built in deploy.yml must carry k8s_psa_labels. It is the "
        "namespace holding 241 manifests, so leaving it unlabelled measures nothing."
    )
    assert "create namespace" not in deploy, (
        "`kubectl create namespace --dry-run=client` cannot carry labels. The namespace must "
        "be applied from a manifest that owns them, or the next apply drops them again."
    )


def test_namespace_apply_reports_a_label_change_as_changed():
    """`created`-only was correct for a label-free manifest and wrong for this one."""
    deploy = (ANSIBLE / "deploy.yml").read_text()
    assert not re.search(
        r"changed_when:\s*\"'created' in deploy_k8s_ns_apply", deploy
    ), (
        "The namespace apply must not report changed only on `created`. A label change comes "
        "back as `configured`, so that test reports ok for a namespace whose PSA labels just "
        "moved — the change becomes invisible in the play output."
    )
