"""The write-verb census row of target slice H1.1h: every task in `setup/k3s` that writes the cluster.

H1 hands the cluster's own plumbing to Flux, one tag per PR, and each PR deletes the
`setup/k3s` tasks that wrote the objects it moves. The plan is
`docs/target-architecture/host/h1-cluster-config-to-flux.md`, H1.1h. This row counts those
writers per task file. Its `allow` map starts with every file that holds one, each naming the PR
that removes its writers, and each H1 PR drops its entry in the same commit. At the end of H1
only `storage_smoke.yml` and `agent_verify.yml` keep an entry.

The predicate is `ansible/tests/_k3s_cluster_writes.py`, which
`ansible/tests/setup/test_k3s_stale_checkout_map.py` shares. The row sits in its own module
because `test_census_rows_roles.py`, which imports it into `ROWS`, is at the cap for a test
module.
"""

from _k3s_cluster_writes import cluster_writes
from _row_table import Census, Subject, tracked

_TASKS = "ansible/roles/setup/k3s/tasks/"

K3S_WRITE_ALLOW = {
    _TASKS + "agent_verify.yml": (
        "stays: it patches the joining node's own Longhorn Node CR, which longhorn-manager "
        "creates at runtime and no Flux tag declares"
    ),
    _TASKS + "storage_smoke.yml": (
        "stays: the opt-in smoke test creates and deletes a throwaway PVC, which no Flux tag "
        "declares"
    ),
    _TASKS + "cluster-addons.yml": (
        "H1.2 hands the PriorityClasses to `cluster-base` and H1.3 hands MetalLB to `metallb`; "
        "each deletes its writers"
    ),
    _TASKS + "kubeconfig.yml": (
        "H1.2 hands the read-only identity to `cluster-base`; the file keeps its host half"
    ),
    _TASKS + "coredns.yml": (
        "H1.4 hands the Corefile key and the Deployment to `coredns`; the file keeps the read "
        "and the marker task"
    ),
    _TASKS + "longhorn.yml": (
        "H1.5a deletes the install, the manager patch and the StorageClass tasks, H1.5b the "
        "Settings, `longhorn-nobackup` and the NetworkPolicy, and H1.6 the label and CSI tasks "
        "with the rest of the file"
    ),
    _TASKS + "longhorn-weekly-shard.yml": (
        "H1.6 deletes the file; `longhorn-reconcile` takes the shard labels over"
    ),
    _TASKS + "longhorn-backup.yml": (
        "H1.7a deletes the volume routing, and H1.7b the credentials, targets and "
        "RecurringJobs with the rest of the file"
    ),
}


def _task(body: str) -> Subject:
    return Subject(_TASKS + "new.yml", body)


K3S_WRITE_VERBS = Census(
    name="k3s-role-cluster-writes-are-counted",
    reason=(
        "Each H1 PR hands a set of cluster objects to Flux and deletes the `setup/k3s` tasks "
        "that wrote them, in the same commit. A writer left behind fights Flux on every hand "
        "apply. A writer in a file that has no allow entry is new: hand its objects to a Flux "
        "tag rather than writing them from this role, or give the file an entry that names the "
        "PR removing it. Each writer outside storage_smoke.yml and agent_verify.yml also needs a "
        "row in tasks/stale-checkout-guard.yml (test_k3s_stale_checkout_map.py)."
    ),
    files=lambda: tracked(_TASKS + "*.yml"),
    offence=cluster_writes,
    red=(
        _task(
            "- name: Apply it\n  ansible.builtin.command:\n"
            "    cmd: k3s kubectl -n {{ ns }} apply -f /etc/rancher/k3s/x.yaml\n"
        ),
        # An `argv:` list, one token per line: the shape a line grep missed.
        _task(
            "- name: Label it\n  ansible.builtin.command:\n    argv:\n      - k3s\n"
            "      - kubectl\n      - -n\n      - longhorn-system\n      - label\n"
            "      - volumes.longhorn.io\n"
        ),
        # `set image`: the other shape a grep for apply/patch/delete missed.
        _task(
            "- name: Pin the image\n  ansible.builtin.command:\n"
            "    cmd: k3s kubectl -n kube-system set image deployment/coredns coredns=x\n"
        ),
        _task(
            "- block:\n    - name: Restart it\n      ansible.builtin.shell: >-\n"
            "        k3s kubectl -n x rollout restart deploy/y\n"
        ),
        _task(
            "- name: Apply with the module\n  kubernetes.core.k8s:\n    state: present\n"
        ),
        # A global flag whose value is a separate word, before the verb.
        _task(
            "- name: Apply slowly\n  ansible.builtin.command:\n"
            "    cmd: k3s kubectl --request-timeout 30s apply -f /x.yaml\n"
        ),
        _task(
            "- name: Delete as admin\n  ansible.builtin.command:\n"
            "    cmd: k3s kubectl --as admin delete pod y\n"
        ),
        _task(
            "- name: Reconcile RBAC\n  ansible.builtin.shell: >-\n"
            "    k3s kubectl auth reconcile -f /x.yaml\n"
        ),
        _task(
            "- name: Approve the CSR\n  ansible.builtin.command:\n"
            "    cmd: k3s kubectl certificate approve csr-1\n"
        ),
        _task(
            "- name: Apply raw\n  ansible.builtin.raw: k3s kubectl apply -f /x.yaml\n"
        ),
        _task("- name: Run a script\n  ansible.builtin.script: apply.sh\n"),
        _task(
            "- name: Patch over HTTP\n  ansible.builtin.uri:\n"
            "    url: https://127.0.0.1:6443/apis/apps/v1/namespaces/x/deployments/y\n"
            "    method: PATCH\n"
        ),
    ),
    green=(
        _task(
            "- name: Wait for the CR\n  ansible.builtin.command:\n"
            "    cmd: k3s kubectl -n longhorn-system wait --for=create nodes.longhorn.io/x\n"
        ),
        _task(
            "- name: Wait for the rollout\n  ansible.builtin.command:\n"
            "    cmd: k3s kubectl -n kube-system rollout status deployment/coredns\n"
        ),
        _task(
            "- name: Read it\n  ansible.builtin.command:\n    argv:\n      - k3s\n"
            "      - kubectl\n      - get\n      - pvc\n"
        ),
        _task(
            "- name: Read with the module\n  kubernetes.core.k8s_info:\n    kind: Pod\n"
        ),
        _task(
            "- name: Ask\n  ansible.builtin.command:\n"
            "    cmd: k3s kubectl --as admin auth can-i get pods\n"
        ),
        # A POST that is not to the API server, such as a heartbeat push.
        _task(
            "- name: Push\n  ansible.builtin.uri:\n"
            "    url: http://uptime-kuma:3001/api/push/x\n    method: POST\n"
        ),
        _task(
            "- name: Read over HTTP\n  ansible.builtin.uri:\n"
            "    url: https://127.0.0.1:6443/api/v1/namespaces\n"
        ),
    ),
    # The three writers H1 keeps: storage_smoke.yml's two and agent_verify.yml's one.
    min_matches=3,
    must_find=frozenset({_TASKS + "storage_smoke.yml", _TASKS + "agent_verify.yml"}),
    allow=K3S_WRITE_ALLOW,
    count=lambda subject: len(cluster_writes(subject)),
)
