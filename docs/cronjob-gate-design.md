# k8s/cronjob-gate — why the gate is shaped as it is

The role's caller contract is `ansible/roles/k8s/cronjob-gate/CLAUDE.md`: what a caller's
CronJob must guarantee, and what a green gate does and does not prove. This page is the
working-out behind that contract — the decisions a session changing the role has to not
undo, kept off the file the inject hook loads on every touch of the role (#2985, the same
split as monitor-bridge's check history and volume-snapshot's drill record).

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
anything — it only lets the failure message say `the new image could not start` rather than
`unrecognised state`, so a missing entry costs wording, never a missed failure.

There is deliberately **no per-caller opt-out**. A switch that let a caller turn the gate off
would be an exemption-shaped hole, and this slice deleted two of those already. Narrowing what
the gate claims is the right move; letting a caller opt out of the claim is not.

`ansible/tests/deploy/test_cronjob_gate_decision.py` pins the split against synthetic container states.
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
created two tasks above, which the same guard skips; reading the previous Job of that deploy
and calling it `this run finished` would be a lie, and left unguarded every dry run burned the
whole timeout budget and then failed.

## Why the internal guard is the only option here

A role reached as a dependency is invisible to anything keyed on `ansible_run_tags`, so it
must guard itself on `k8s_no_mutate`. `volume-claim` and `image-builder` are in exactly this
position; this role is the same shape. The `k8s_dry_run_unsupported` refusal list, the
alternative for a NAMED role, went empty and was deleted in #2876.

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
