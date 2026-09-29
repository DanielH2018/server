# k8s/cronjob-gate — a deploy-time gate for a CronJob-only role

This role deploys nothing. A caller includes it, and it makes one run of the caller's CronJob
happen *now*, then blocks the play until that run reaches a terminal state.

**No standalone deploy tag.** Callers reach it via `include_role: name: k8s/cronjob-gate`,
never `--tags cronjob-gate` — see the invocation example below.

**What it proves is narrow, and worth reading before you rely on it: this role proves the new
image RUNS. It does not prove the workload succeeded.** That is weaker than the Deployment path,
where `roles/k8s/manifests` runs `rollout status` and then a stability soak against a
readinessProbe. Here, a run whose container never started fails the deploy; a run whose container
started and exited non-zero is reported and the deploy continues. "Why the split is drawn there"
below explains why, and it is a decision rather than an omission.

```yaml
- name: Gate the widget deploy on a one-off run
  tags: [deploy]
  ansible.builtin.include_role:
    name: k8s/cronjob-gate
  vars:
    cronjob_gate_name: widget          # the CronJob's metadata.name
    # cronjob_gate_timeout: 300        # seconds; default in defaults/main.yml
```

## Why a CronJob needs this at all

A Deployment is gated by `roles/k8s/manifests`, which runs `rollout status` and then a
stability soak. A CronJob has no rollout. `kubectl apply` writes the new spec, the play reports
success, and **nothing executes** — the first thing to run the new image is the next scheduled
firing, hours later. So an image bump that cannot start, a broken config, or a missing secret
all deploy green and stay invisible until the schedule comes round, at which point the failure
is a monitor's problem rather than a deploy's.

A Job can be gated with `kubectl wait --for=condition=complete job/<name>` after the apply.
A CronJob cannot: there is no Job to name. This role supplies the missing half by creating one
imperatively from the CronJob — `kubectl create job <name>-deploy-gate --from=cronjob/<name>` —
which copies the CronJob's pod template exactly, so the run exercises the same image, the same
config and the same volumes the schedule will.

`ansible/tests/deploy/test_k8s_autodeploy_batch_gates.py` knows about this role: a role that renders a
CronJob and includes `k8s/cronjob-gate` with a matching `cronjob_gate_name` counts as gating
that workload, and is therefore allowed to declare `k8s_autodeploy: true`.

## What a caller's CronJob must guarantee

Both of these must be **re-checked for every new caller**, because this role makes the CronJob's
real workload run on every deploy — not a probe, not a dry run, the actual job.

1. **`concurrencyPolicy: Forbid`.** It protects one direction of the overlap and not the other,
   so read which one you are getting. `concurrencyPolicy` governs whether the CronJob
   **controller** creates a *scheduled* Job; `kubectl create job --from=cronjob` writes a Job
   directly and no admission path consults it.

   * **Protected:** a gate run in flight suppresses the next scheduled firing. Verified live
     2026-08-21 — `longhorn-system/postmig-d6b`, a `cronjob.kubernetes.io/instantiate: manual`
     Job, carries `ownerReferences[0].kind: CronJob` with `controller: true`, which is what puts
     it in the CronJob's `.status.active` for `Forbid` to key on.
   * **Unprotected:** a gate run created *into* a scheduled run that is already going. Nothing
     refuses that, so property 2 below is what makes it safe, not `Forbid`.

   The unprotected window is the scheduled run's own duration, and it is small — live
   `lastScheduleTime` → `lastSuccessfulTime` is 21s for pi-peer-backup and 26s for configarr
   (both measured 2026-08-21). Small, not zero: a deploy landing inside it runs the workload
   twice against the same state.
2. **Idempotence under one extra out-of-band run.** The schedule is the CronJob's contract; this
   role breaks it by adding a run at an arbitrary time. A convergent reconciler (configarr) or a
   retention-free mirror (pi-peer-backup) does not care. A job that rotates a credential,
   appends to a ledger, prunes by count, or bills something does — do not gate one of those with
   this role.

**`cronjob_gate_timeout` must exceed the caller's `activeDeadlineSeconds`.** Set that way, a
hung run hits its deadline first, the Job goes `Failed`, the poll sees it, and the deploy fails
with this role's own message naming what it could and could not read. Set the other way, the
poll's retries run out first and Ansible aborts with a bare "ran out of retries" — no message,
no states, nothing to act on. `ansible/tests/deploy/test_cronjob_gate_decision.py` enforces the
ordering against every caller's rendered CronJob, because the prose version of this rule was
violated by the shipped default on the day it was written.

**The deadline path carries no log, and that is not fixable here.** When
`activeDeadlineSeconds` fires, the Job controller *deletes* the active pods — so the container
states and `kubectl logs` are both gone by the time the poll returns. The failure message says
so rather than promising a log it cannot produce. What the ordering rule buys is a clear
failure instead of an opaque one, not a diagnostic.

**So a hung dependency fails the deploy where a fast one does not.** An *arr that is briefly
down makes configarr exit non-zero in seconds, which is reported and the deploy continues. An
*arr that hangs until the deadline leaves no readable state, which fails closed. Same root
cause, two outcomes, decided by how the dependency fails rather than by what it is. That
asymmetry survived the extraction from configarr and is a real limit of reading terminal state
after the fact — an operator hitting it should look at the *arr, not at the image.

## The design decisions behind it

`docs/cronjob-gate-design.md` carries the working-out, and a change to this role's behaviour
is a change to something on that page:

- **Why the split is drawn there** — the three-row classification table, the
  `cronjob_gate_ran_reasons` allowlist and why there is no per-caller opt-out.
- **Why it polls instead of using `kubectl wait`** — `backoffLimit: 0` settles a failure in
  seconds that `wait --for=condition=complete` would sit out for the whole timeout.
- **The guard is `k8s_no_mutate`** — why the narrow `ansible_check_mode` guard would let a
  `--dry-run` fire a real reconcile, and why the poll is skipped rather than run.
- **Provenance** — extracted from `roles/k8s/configarr`, with the two deliberate changes.
