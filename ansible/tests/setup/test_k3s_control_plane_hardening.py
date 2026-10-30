#!/usr/bin/env python3
"""Guards the three control-plane hardening controls.

Each test here encodes a way one of them fails while every surface still reads green. The
flags are `k3s_server_config` in `hosts/components/k3s-server/component.yml` since H6.4b, so
the tests read the config drop-in daniel-box's package ships, which is what k3s reads.

  * Two sources for one switch. `k3s_secrets_encryption` stays in setup/k3s for server.yml's
    status and rotate-keys tasks until H7, and the flag itself is in the component. A flag
    turned off with the role key left on makes those tasks assert against a cluster that no
    longer encrypts.

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

import functools

from lib import yaml_fast
from _helpers import ALL_VARS
from _helpers import load_yaml, load_defaults
from _k8s_render import render_role_template, rendered_k8s_text
from _setup_render import role_context
from lib.repo_paths import K3S_ROLE, REPO
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.bundle import homelab_namespace_files
from renderer.renderer_lib.namespace import HOMELAB_NAMESPACE

AUDIT_POLICY = "/var/lib/rancher/k3s/server/audit.yaml"
SERVER_CONFIG = "/etc/rancher/k3s/config.yaml.d/50-homelab.yaml"
COMPONENT = REPO / "hosts" / "components" / "k3s-server" / "component.yml"


def _all_vars() -> dict:
    return load_yaml(ALL_VARS)


@functools.cache
def _package():
    return host_mode.render_host("daniel-box")


def _server_config() -> dict:
    """The server's flags as daniel-box's package ships them in config.yaml.d (H6.4b)."""
    return yaml_fast.safe_load(_package().files[SERVER_CONFIG][0].decode())


def _apiserver_arg_names() -> set[str]:
    return {arg.split("=", 1)[0] for arg in _server_config()["kube-apiserver-arg"]}


def secrets_encryption_drift(component: dict, role: dict) -> bool:
    """True when the component's flag and setup/k3s's switch disagree."""
    flag = component["k3s_server_config"].get("secrets-encryption", False)
    return flag is not role["k3s_secrets_encryption"]


def _server_tasks() -> list[dict]:
    return yaml_fast.safe_load((K3S_ROLE / "tasks" / "server.yml").read_text()) or []


def _task_index(predicate) -> int:
    for i, task in enumerate(_server_tasks()):
        if predicate(task):
            return i
    return -1


@functools.cache
def _audit_policy() -> dict:
    """The audit policy daniel-box's package ships, as host mode renders it (H6.4a).

    The package ships the file before any k3s start can name it, so the old "written before the
    installer runs" ordering test left with the task. `test_k3s_server_component.py` holds that
    every file a flag names is a file the package ships.
    """
    return yaml_fast.safe_load(_package().files[AUDIT_POLICY][0].decode())


# ── G1: secrets encryption ──────────────────────────────────────────────────────────────


def test_secrets_encryption_is_armed():
    assert _server_config().get("secrets-encryption") is True, (
        "k3s_server_config must set secrets-encryption. Without it every Secret is stored "
        "in etcd as plaintext, and etcd-snapshot-offbox.sh ships a daily copy of that to R2."
    )


def test_secrets_encryption_equals_the_role_switch():
    """server.yml's status and rotate-keys tasks read the role key; k3s reads the flag."""
    component = yaml_fast.safe_load(COMPONENT.read_text())["defaults"]
    role = load_defaults(K3S_ROLE)
    assert not secrets_encryption_drift(component, role), (
        "secrets-encryption in k3s_server_config must equal setup/k3s's "
        "k3s_secrets_encryption. Change both in one PR."
    )
    flipped = {"k3s_server_config": {"secrets-encryption": False}}
    assert secrets_encryption_drift(flipped, role), "the drift check cannot go red"


def test_existing_secrets_are_reencrypted():
    """Arming the flag encrypts WRITES only; without this the old plaintext stays."""
    text = (K3S_ROLE / "tasks" / "server.yml").read_text()
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
    args = _apiserver_arg_names()
    for flag in ("audit-policy-file", "audit-log-path"):
        assert flag in args, (
            f"k3s_server_config must pass kube-apiserver-arg {flag}=… . The apiserver keeps "
            "no audit log at all without both."
        )


def test_audit_log_rotation_is_configured():
    """An unrotated audit log fills the disk disk-health.sh watches."""
    args = _apiserver_arg_names()
    for flag in ("audit-log-maxage", "audit-log-maxbackup", "audit-log-maxsize"):
        assert flag in args, (
            f"k3s_server_config must pass kube-apiserver-arg {flag}=… . The apiserver does "
            "not rotate its audit log by default, so a Metadata-level policy on a cluster "
            "this chatty grows without bound on the control-plane disk."
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

    Position is the whole test. A policy whose first rule drops every `get`/`list`/`watch`
    unconditionally makes a later Metadata rule naming secrets match nothing — a `kubectl get
    secrets` the readonly SA was refused would leave no trace at all. Asserting only that some
    rule logs secrets passes with that bug live.
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


def test_alloy_tails_the_audit_log():
    """A log nobody can read is not a control — and the tail has two halves.

    Both halves read the rendered PATH rather than the variable's name in the template. The
    name appearing anywhere — a comment, a different directive — satisfied the old form, and the
    rendered path is what Alloy and the kubelet actually get (#3202).
    """
    # Resolved, not raw: `k3s_audit_log_path` is itself a template over `k3s_audit_log_dir`,
    # so group_vars holds `{{ k3s_audit_log_dir }}/audit.log` and only the render context
    # expands it.
    resolved = role_context(K3S_ROLE)
    path, directory = resolved["k3s_audit_log_path"], resolved["k3s_audit_log_dir"]
    # The Alloy config sits in a nested `templates/config/` directory, which the whole-tree
    # render does not glob; `render_role_template` addresses it by its loader-relative name.
    config = render_role_template("loki-homelab", "config/config.alloy.j2")
    assert path in config, (
        f"the Alloy config must declare a file source for {path} (k3s_audit_log_path). "
        "Otherwise the audit log is readable only as root on daniel-box, which means in "
        "practice nobody reads it."
    )
    daemonset = rendered_k8s_text("loki-homelab", "alloy-daemonset.yaml.j2")
    assert directory in daemonset, (
        f"the Alloy DaemonSet must hostPath-mount {directory} (k3s_audit_log_dir). A file "
        "source whose path is not mounted into the container tails nothing and reports no "
        "error — the job just quietly finds no targets."
    )


def test_audit_log_vars_are_visible_to_both_roles():
    """A role default cannot be read by another role's template."""
    shared, role = _all_vars(), load_defaults(K3S_ROLE)
    for name in ("k3s_audit_log_enabled", "k3s_audit_log_dir", "k3s_audit_log_path"):
        assert name in shared, (
            f"{name} must live in group_vars/all.yml. setup/k3s writes the log and "
            "loki-homelab tails it; as a role default the second role sees only the "
            "`| default(false)` fallback, so the shipper silently never tails the file."
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
    """Labels on one namespace and not the other leave the two namespaces enforcing different policy."""
    obs = rendered_k8s_text("observability", "00-namespace.yaml.j2")
    # The rendered labels, not the variable's name: a Namespace that mentions k8s_psa_labels in
    # a comment and labels nothing passed the old form.
    missing = sorted(
        f"{key}: {value}"
        for key, value in _all_vars()["k8s_psa_labels"].items()
        if f"{key}: {value}" not in obs
    )
    assert missing == [], (
        "The observability Namespace must render every k8s_psa_labels entry, or the two "
        f"namespaces diverge and only one of them reports violations. Missing: {missing}"
    )
    # The workload Namespace's only writer is the shared kind homelab-namespace (D5 step 30).
    workload = homelab_namespace_files("600s")[
        f"cluster/{HOMELAB_NAMESPACE}/namespace.yaml"
    ]
    missing = sorted(
        f"{key}: {value}"
        for key, value in _all_vars()["k8s_psa_labels"].items()
        if f"{key}: {value}" not in workload
    )
    assert missing == [], (
        "The homelab Namespace that homelab-namespace renders must carry every "
        "k8s_psa_labels entry. It is the namespace holding 241 manifests, so leaving it "
        f"unlabelled measures nothing. Missing: {missing}"
    )
