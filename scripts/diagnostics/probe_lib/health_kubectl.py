"""The kubectl argv builders and the pod selector `probe.py health` runs its queries through.

Split out of probe_lib/health.py, which had grown to 938 lines. Pure argv construction plus one
label-expression builder: nothing here runs a command, reads a file or imports a sibling, so
every shape the gate depends on can be asserted without a cluster.

health.py keeps the gate itself. `health_rollout.py` formats a Deployment/DaemonSet verdict,
`health_cronjob.py` a CronJob one, and `health_docker.py` covers the Pi's remaining Docker
services.

The ClusterIP lookups — `k8s_service_ip_argv` and `resolve_service_ip` — are kubectl but live
in `health_docker.py` beside the bridge-IP pair they replaced. Read the `# DECIDED:` marker
above them before moving them here: `resolve_service_ip` runs a subprocess, which the paragraph
above rules out for this module.
"""

# The kinds a rollout gate can actually check, and kubectl's spelling for each. Deployment
# first because the fleet is overwhelmingly Deployments. StatefulSet is here with no live
# instance today on purpose: resolving a kind the lookup below cannot ask for would make the
# first StatefulSet added to this repo read as MISSING.
WORKLOAD_KINDS = {
    "Deployment": "deploy",
    "DaemonSet": "daemonset",
    "StatefulSet": "statefulset",
}


def k8s_deploy_argv(service, namespace, kind="deploy"):
    """kubectl argv to fetch a Deployment/DaemonSet/StatefulSet as JSON.

    Args:
        service: The workload name.
        namespace: The k8s namespace it runs in.
        kind: The workload kind to fetch (``deploy``, ``daemonset`` or ``statefulset``).
    """
    return [
        "k3s",
        "kubectl",
        "-n",
        namespace,
        "get",
        kind,
        service,
        "-o",
        "json",
    ]


def k8s_pods_argv(service, namespace, selector=None):
    """kubectl argv for a workload's pods.

    `selector` overrides the `app=<service>` guess — pass `pod_selector(workload)` whenever the
    workload document is in hand.
    """
    return [
        "k3s",
        "kubectl",
        "-n",
        namespace,
        "get",
        "pods",
        "-l",
        selector or f"app={service}",
        "-o",
        "json",
    ]


def k8s_cronjob_argv(name, namespace):
    """kubectl argv to fetch a CronJob as JSON."""
    return ["k3s", "kubectl", "-n", namespace, "get", "cronjob", name, "-o", "json"]


def k8s_jobs_argv(namespace):
    """kubectl argv for every Job in a namespace, as JSON.

    Not filtered server-side: a CronJob's Jobs carry no label naming their owner, only an
    `ownerReferences` entry, and kubectl has no field selector for that. The namespaces this
    runs against (`homelab`, `longhorn-system`) hold at most a handful of Jobs each — bounded
    by each CronJob's `successfulJobsHistoryLimit` — so filtering client-side in
    latest_owned_job costs nothing worth avoiding.
    """
    return ["k3s", "kubectl", "-n", namespace, "get", "jobs", "-o", "json"]


def k8s_job_pods_argv(job_name, namespace):
    """kubectl argv for one Job's pods, as JSON.

    `batch.kubernetes.io/job-name`, not the deprecated bare `job-name` — the same choice
    `roles/k8s/cronjob-gate/tasks/main.yml` documents and makes, for the same reason: if the
    legacy label is ever dropped, the other selector silently matches nothing.
    """
    return [
        "k3s",
        "kubectl",
        "-n",
        namespace,
        "get",
        "pods",
        "-l",
        f"batch.kubernetes.io/job-name={job_name}",
        "-o",
        "json",
    ]


def pod_selector(workload):
    """The `-l` expression matching a workload's OWN pods, read from its pod template labels.

    Not `app=<name>`. pihole-2's Deployment selects `app: pihole`, so `app=pihole-2` matched
    no pods at all while `app=pihole` matched both piholes' — and a pod query that matches
    nothing yields `restarts=0` with an empty recent-restart list, which is byte-identical to a
    genuinely quiet workload. That would leave the restart half of this gate silently inert,
    the half that caught a crashlooping kube-state-metrics on 2026-08-07.

    Nor `spec.selector.matchLabels`, which is what this read until 2026-09-02. `spec.selector`
    is `apps/v1` immutable, so a role running two instances off one pod template cannot
    discriminate them there: both pihole Deployments select `app: pihole`, and each therefore
    read the union of the two instances' pods. The pod template is where the discriminating
    label can live — pihole's carries `instance: pihole` / `instance: pihole-2` for exactly
    that reason (`roles/k8s/pihole/templates/deployment.yaml.j2`), and the pihole web Service
    already selects on it. k8s requires `spec.template.metadata.labels` to be a superset of
    `spec.selector.matchLabels`, so this is never wider than the selector.

    It can be narrower than the running set during a deploy that changes a pod template label:
    pods from the previous revision do not carry the new label and drop out of the query. The
    rollout half of the gate still sees them, because that half reads the Deployment's own
    replica counts rather than this query.

    Falls back to the selector, then to the `app=<name>` guess, for a workload carrying
    neither — which the k8s API rejects, so it is unreachable. An empty `-l` would query every
    pod in the namespace, which is worse than the guess.
    """
    spec = (workload or {}).get("spec") or {}
    template = ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
    matched = template or (spec.get("selector") or {}).get("matchLabels") or {}
    return ",".join(f"{key}={value}" for key, value in sorted(matched.items()))
