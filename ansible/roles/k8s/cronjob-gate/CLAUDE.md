# k8s/cronjob-gate — a deploy-time gate for a CronJob-only role

This role deploys nothing. A caller includes it, and it makes one run of the caller's CronJob
happen *now*, then blocks the play until that run reaches a terminal state.

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

`ansible/tests/test_k8s_autodeploy_batch_gates.py` knows about this role: a role that renders a
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
no states, nothing to act on. `ansible/tests/test_cronjob_gate_decision.py` enforces the
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

## Why the split is drawn there

A gate that failed the deploy on *any* unsuccessful run would be wrong here, and the repo has
the scar: `configarr/tasks/main.yml` records that a wrapper was retired precisely because it
failed deploys over transient *arr outages, and it kept the sync task non-enforcing for that
reason. Reversing that wholesale to build this gate would have re-created the problem.

What makes a narrower gate possible is *when auto-deploy fires*: only on an `_image:`-only diff.
The single thing that changed is the image, so the two failure modes have different owners.

| the container… | on an image-only diff that is… | so the gate… |
|---|---|---|
| never reached its entrypoint — pull failure, bad exec format, a config the kubelet could not turn into a container | unambiguously the new image's fault, and the caller's health monitor takes hours to notice | **fails the deploy** |
| ran and exited non-zero | almost never the image — a transient dependency, which the caller's own monitor already pages for on a bounded delay | reports it, shows the log, continues |
| left no readable state, or a state this role does not recognise | unknown | **fails the deploy** (fail-closed) |

The classification lives in `defaults/main.yml` as `cronjob_gate_ran_reasons`, and it is an
**allowlist**: fatal unless every container state read is one of `Completed` or `Error`. Written
that way round so a reason string Kubernetes adds later cannot quietly land on the non-fatal
side. `cronjob_gate_start_failure_reasons` is the companion list and does **not** decide
anything — it only lets the failure message say "the new image could not start" rather than
"unrecognised state", so a missing entry costs wording, never a missed failure.

There is deliberately **no per-caller opt-out**. A switch that let a caller turn the gate off
would be an exemption-shaped hole, and this slice deleted two of those already. Narrowing what
the gate claims is the right move; letting a caller opt out of the claim is not.

`ansible/tests/test_cronjob_gate_decision.py` pins the split against synthetic container states.
It exercises the decision, not the deploy: `kubectl` here is read-only, so the live path from a
broken image through to a failed play is unexercised.

## Why it polls instead of using `kubectl wait`

`kubectl wait` can name only one condition. With `backoffLimit: 0` — which a gated CronJob
generally wants, so a failure is reported rather than retried — a failed run settles in seconds,
but `wait --for=condition=complete` would sit there for the whole timeout before saying so. So
the role polls `kubectl get job … -o jsonpath={.status.conditions[*].type}` with an `until:` that
accepts **either** terminal condition, and decides afterwards which one it got.

`k8s/image-builder` solves the same problem as
`wait --for=condition=complete || wait --for=condition=failed`. That works, but it costs an extra
10 s on every failure and reads as two gates rather than one.

The poll carries `failed_when: false`, and it is narrower than it looks. On the `Failed` path
`kubectl get` returns rc 0 — it found the Job and printed its conditions — so nothing would abort
even without it; it covers a non-zero rc on the attempt that satisfied `until`, which would
otherwise fail the task before the log dump could run. It is not a suppressor either way: a run
reaching **neither** condition exhausts the retries and Ansible fails the task regardless of
`failed_when`, so a hang still fails the deploy — just without a log.

That last case is what the timeout rule above exists to keep out of the way.

## The guard is `k8s_no_mutate`, not `ansible_check_mode` or `k8s_dry_run`

Every mutating task carries `when: not (k8s_no_mutate | bool)`.
`inventory/group_vars/all.yml` defines that as `ansible_check_mode or (k8s_dry_run | bool)`,
and the reason it is one fact rather than two is that a role guarding either one alone is
guarded against neither in practice — image-builder was fully `--check`-clean and would still
have built and pushed an image under a dry run. This role issues `kubectl create job`, so the
narrow guard would make `./scripts/deploy.sh --dry-run` fire a real reconcile into the *arr
stack, or a real rsync from the Pi.

The poll is **skipped** under that guard rather than forced to run. The Job it waits on is
created two tasks above, which the same guard skips; reading the previous deploy's Job and
calling it "this run finished" would be a lie, and left unguarded every dry run burned the whole
timeout budget and then failed.

## Why it is not in `k8s_dry_run_unsupported`

That list makes `deploy.yml` refuse a dry run rather than half-apply, but the refusal keys on
`ansible_run_tags` — so it only ever reaches roles the operator NAMES on the command line. A
shared role reached as a dependency is invisible to it. `seed-volume` and `image-builder` are in
exactly this position and are guarded internally on `k8s_no_mutate` instead of listed; this role
is the same shape.

## Provenance

Extracted from `roles/k8s/configarr`, which had carried this inline since it ported to k3s. The
mechanism was not designed here — only lifted, with two deliberate changes:

- the guard widened from `ansible_check_mode` to `k8s_no_mutate`;
- a run whose container **never started** now fails the deploy. configarr's inline version left
  every failure to the monitor, so a fast failure passed the play while a hang failed it — the
  same outcome reached two opposite ways. The application-failure half of its decision is
  preserved deliberately, for the reason its own comment gives.

The Job is named `<cronjob_gate_name>-deploy-gate`; configarr's inline version named it
`configarr-deploy`.
