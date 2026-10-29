"""A Failed pod the weekly reboot leaves in an add-on namespace has something that deletes it.

#6367 gave `homelab` two cleanups: `ttlSecondsAfterFinished` on every in-window CronJob
(`test_reboot_window_cronjobs_age_out.py`) and the `dri-admission-reaper` CronJob, whose Role
grants only in `homelab`. The add-on namespaces run Jobs this repo does not render, so neither
reaches them: k3s's helm-install Jobs in `kube-system` and Longhorn's backup Jobs in
`longhorn-system` (#6460). A pod the reboot fails there stays Failed, because the
controller-manager's `terminated-pod-gc-threshold` is 12500 pods.

Covered means the render holds a CronJob whose ServiceAccount may `list` and `delete` pods in
that namespace, through a Role, or through a ClusterRole bound there or cluster-wide. A grant no
CronJob runs as deletes nothing, so it does not count.

`system-upgrade` is left out on purpose: the vendored controller's `default-controller-env`
sets `SYSTEM_UPGRADE_JOB_TTL_SECONDS_AFTER_FINISH` to 900, and no patch overrides it, so a SUC
Plan Job ages out on its own.

Run: uv run pytest ansible/tests/k8s/test_failed_pods_outside_homelab_have_a_cleanup.py
"""

import pytest
from _k8s_render import rendered_docs

# The add-on namespaces whose Job pods no rendered TTL or grant reaches (#6460).
UNREAPED_NAMESPACES = ("kube-system", "longhorn-system")


def _admits(values, wanted: str) -> bool:
    values = values or []
    return wanted in values or "*" in values


def _deletes_pods(rules) -> bool:
    """Whether one rule in `rules` grants both `list` and `delete` on core pods."""
    return any(
        _admits(rule.get("apiGroups"), "")
        and _admits(rule.get("resources"), "pods")
        and _admits(rule.get("verbs"), "list")
        and _admits(rule.get("verbs"), "delete")
        for rule in rules or []
    )


def reaper_accounts(docs: list[dict], namespace: str) -> set[tuple[str, str]]:
    """The (namespace, name) ServiceAccounts that may list and delete pods in `namespace`."""
    roles = {
        (doc["kind"], doc["metadata"].get("namespace"), doc["metadata"]["name"])
        for doc in docs
        if doc.get("kind") in ("Role", "ClusterRole")
        and _deletes_pods(doc.get("rules"))
    }
    accounts = set()
    for doc in docs:
        kind = doc.get("kind")
        if kind not in ("RoleBinding", "ClusterRoleBinding"):
            continue
        bound_in = doc["metadata"].get("namespace")
        if kind == "RoleBinding" and bound_in != namespace:
            continue
        ref = doc.get("roleRef") or {}
        ref_ns = bound_in if ref.get("kind") == "Role" else None
        if (ref.get("kind"), ref_ns, ref.get("name")) not in roles:
            continue
        for subject in doc.get("subjects") or []:
            if subject.get("kind") == "ServiceAccount":
                accounts.add((subject.get("namespace") or bound_in, subject["name"]))
    return accounts


def cronjob_accounts(docs: list[dict]) -> set[tuple[str, str]]:
    """The (namespace, ServiceAccount) every rendered CronJob's pods run as."""
    found = set()
    for doc in docs:
        if doc.get("kind") != "CronJob":
            continue
        pod = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        found.add(
            (doc["metadata"].get("namespace"), pod.get("serviceAccountName", "default"))
        )
    return found


@pytest.mark.parametrize("namespace", UNREAPED_NAMESPACES)
def test_a_cronjob_may_delete_the_failed_pods_in_the_namespace(namespace: str) -> None:
    docs = [doc for _, _, doc in rendered_docs()]
    runners = reaper_accounts(docs, namespace) & cronjob_accounts(docs)
    assert runners, (
        f"no rendered CronJob runs as a ServiceAccount that may list and delete pods in "
        f"{namespace}, so a Job pod the reboot fails there stays Failed (#6460)"
    )
