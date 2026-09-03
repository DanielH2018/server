# k8s/rollout-drain — the batched, concurrent rollout wait

This role deploys nothing. `ansible/tasks/k8s_batch.yml` includes it at the end of every
batch to wait, concurrently, on every rollout queued by the batch's roles
(`k8s_pending_rollouts`), then snapshot restart counts for the deferred stabilisation
gate.

**No standalone deploy tag.** Every task here is `tags: [always]`, not `[deploy]` —
never `--tags rollout-drain`, and that tagging is load-bearing, not cosmetic: `[deploy]`
tasks are filtered out of every gitops-deploy run, so a role that once carried this
logic tagged `[deploy]` waited on nothing while the play reported `failed=0` (measured
on a real `--tags littlelink` deploy: `ok=94, failed=0`, drain executed zero times).

## Why it exists

`k8s/manifests` used to run `kubectl rollout status` inline, serially, right after its
own apply. Measured over a full deploy: 1386s across 31 such waits against 435s of
actual work. The waits were serial because Ansible is serial, not because the cluster
needed them to be — `kubectl apply` is asynchronous, and k3s reconciles every workload
concurrently. This role runs the same waits as background shell jobs instead, one per
queued rollout, each with its own per-role timeout.

## What it is not

Not `kubectl wait --for=condition=Available` — every Deployment here is single-replica
with `maxUnavailable` down to 0, so the OLD pod satisfies `Available` for the whole
rolling update and the wait would return before the new pod is even scheduled. Only
`rollout status` gates on the new ReplicaSet.

The tasks are guarded on an empty queue (`k8s_pending_rollouts | length > 0`), so
`--skip-tags deploy` — which skips the queueing task in `k8s/manifests` — leaves this
role a no-op rather than an error.
