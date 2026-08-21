# k8s/volume-revert — put a service's volumes back to the pre-deploy snapshot

This role deploys nothing. It reverts each of a service's Longhorn volumes to the snapshot
`k8s/volume-snapshot` took immediately before the deploy that failed, so that the rollback's
apply restores manifests over data those manifests can actually read.

**Read this first: the role stops the workload and does not start it again.** It scales the
Deployment to zero, reverts, and leaves it there. The apply that follows in the rollback
restores `replicas: 1` from the manifest — one rollout instead of two, and no race between
them. A caller that reverts without applying afterwards leaves the service down.

**This code path runs only during an incident.** Nothing exercises it on a good day. That is
why every step that can fail on its own account is checked before the scale-down, and why
`ansible/tests/test_volume_revert.py` is mostly ordering tests.

## What the caller passes

```yaml
- name: Revert the volumes for widget
  ansible.builtin.include_role:
    name: k8s/volume-revert
  vars:
    volume_revert_service: widget          # also the Deployment name
    volume_revert_claims: [widget-config]  # PVC names in k8s_namespace
    volume_revert_sha: 1a2b3c4d            # the deploy tag the snapshot was named with
```

`volume_revert_sha` must be the **same string** `k8s/volume-snapshot` resolved for the deploy
being rolled back — the output of `git rev-parse --short=8 HEAD` at that commit, used verbatim.
`--short=8` is a minimum, not a width: git returns more characters when eight are ambiguous, and
the snapshot carries whatever it returned. So this role checks the shape (`^[0-9a-f]{8,}$`) and
transforms nothing. Truncating to eight would fail to match a nine-character name.

## The sequence, and why every step is there

Measured 2026-08-21 on `speedtest-config`, Longhorn v1.12.1. Two plausible shortcuts were
measured and both fail:

| volume state | revert result |
|---|---|
| attached, frontend enabled | `500 failed to revert snapshot for volume … with frontend enabled` |
| plainly detached | `500` — no engine is running, so nothing can perform the revert |
| attached with `disableFrontend: true` | works |

Per claim, in this order:

1. **Resolve the PVC's Longhorn volume.** Also the only proof the claim exists and is bound.
2. **Find the newest snapshot matching `autodeploy-<svc>-<sha>-<claim>-`, and fail when none
   does.** Newest by `creationTimestamp`, never by name — CR names are not chronologically
   sortable as strings. `markRemoved` snapshots are rejected: a snapshot already marked removed
   cannot be reverted to.
3. **Scale the Deployment to zero.** The volume cannot be attached in maintenance mode while a
   pod holds it.
4. **Wait for `state: detached`.**
5. **`POST ?action=attach {hostId, disableFrontend: true}`** — maintenance mode.
6. **Wait for `state: attached`, then assert `spec.disableFrontend` is true.** The assert is a
   precondition, not a formality: without it the revert fails at step 7, after the workload is
   already at zero, and the message names the wrong thing.
7. **`POST ?action=snapshotRevert {name}`**, demanding HTTP 200.
8. **`POST ?action=detach {}`, then wait for `detached`.**

Steps 1 and 2 are reads. They run under `--check` too (`check_mode: false`), so a dry run
answers the question that matters most about a rollback — is there a snapshot to revert to? —
without changing anything.

## The attach and the detach pair on an empty ticket key

Read from longhorn-manager v1.12.1: `manager.Attach` stores the attachment ticket under
whatever `attachmentID` the caller sends, and `manager.Detach` does
`delete(tickets, attachmentID)` and **ignores `hostId` entirely**. So:

- neither call sends an `attachmentID`, both therefore key the ticket `""`, and the detach
  removes the ticket the attach created;
- adding an `attachmentID` to the attach alone would make the detach delete nothing **and
  return HTTP 200 while doing it**, leaving the volume attached with its frontend disabled and
  the workload at zero;
- the detach does not send `hostId`, because the server never reads it and sending it would
  document a guarantee that does not exist.

The wait on `state: detached` after the detach is the only check that a 200 detach actually
detached. It must never be suppressed with `failed_when: false`.

## Maintenance mode does not leak

The drill measured `spec.disableFrontend: false` on the volume after the normal scale-up that
follows a revert. The flag lives on the attachment ticket, and the ticket this role creates is
removed by its own detach; the pod's own attach afterwards is an ordinary one. So a successful
revert leaves nothing to clean up.

A **failed** revert can leave the flag set, because the detach is the last step. See below.

## A multi-claim revert is not atomic

`tdarr` (`tdarr-configs`, `tdarr-server`) and `code-server` (`code-server-config`,
`code-server-workspace`) each hold two claims. Each claim reverts separately, at a slightly
different instant, and a failure on the second stops the play with the first already reverted.
That is deliberate: continuing past a failed revert would be worse. But it means a partial
failure leaves a service's volumes mutually inconsistent — a different, and possibly worse,
state than the bad deploy it was recovering from.

**Recovering a partial revert by hand.** The play stops with the workload at zero replicas and
possibly one volume still attached in maintenance mode. Read the failing task's name to see
which claim it stopped on, then:

1. `kubectl -n longhorn-system get volumes.longhorn.io <vol> -o jsonpath='{.status.state}{"|"}{.spec.disableFrontend}'`
   for each of the service's volumes.
2. Any volume still attached with `disableFrontend: true` needs
   `POST {api}/v1/volumes/<vol>?action=detach {}` against **this node's own** longhorn-manager
   pod IP — the ClusterIP is a coin flip, see `k8s/longhorn-api`.
3. Decide per claim whether to finish the revert or to accept the volumes as they are. Reverting
   the remaining claims by hand is the same sequence as above.
4. Deploy the service to bring it back up: `./scripts/deploy.sh --tags <service>`.

**A timeout is not proof the revert did not happen.** `volume_revert_api_timeout` bounds the
client, not the server: a `snapshotRevert` that timed out may still have been performed. Read
the volume's snapshot chain before retrying rather than assuming nothing changed — a revert
applied twice to the same snapshot is harmless, but a revert applied to a volume that already
moved on is not what the operator thinks they are doing.

## This role is deliberately absent from `k8s_dry_run_unsupported`

It mutates outside `roles/k8s/manifests`, which is normally what puts a role on that list. The
list keys on `ansible_run_tags`, so it only reaches roles an operator names on the command line
— a role reached as a dependency is invisible to it. `seed-volume`, `image-builder`,
`cronjob-gate` and `volume-snapshot` are all in the same position and guard themselves
internally instead. This role does the same: every mutating task and every wait carries
`when: not (k8s_no_mutate | bool)`, which is `ansible_check_mode or (k8s_dry_run | bool)`.

Guarding on either half alone is the bug that guard exists to prevent. `ansible/tests/test_volume_revert.py`
pins the whole census of mutating tasks against it.

## Things measured rather than assumed

- The Longhorn API answers only from the **node-local** manager pod: the longhorn-manager
  NetworkPolicy's `from:` is all podSelectors, so host-originated traffic reaches no other. This
  role never resolves that itself — `k8s/longhorn-api`, included with `tasks_from: resolve.yml`,
  does, once per run and before the first claim is touched.
- `kubectl`'s jsonpath has **no `&&`** — `unrecognized character in action: U+0026`, verified
  2026-08-21. The snapshot listing filters on `spec.volume` alone and does the name prefix and
  `markRemoved` in Jinja. Do not fold them back together.
- An **indexed** jsonpath (`{.items[0]...}`) against a zero-match query returns rc=1 with
  `array index out of bounds`, which aborts the task before any guard runs. `{range .items[…]}`
  returns rc=0 and empty stdout, which is what makes the failure message reachable.
- `argv:`, never a folded `cmd:` string. `ansible.builtin.command` shlex-splits a `cmd`, so an
  argument containing a space is torn in half — slice 4 shipped that and made a branch dead code
  for a round.
- `snapshotRevert` takes a `snapshotInput`, whose only field this needs is `name` — read from
  the server's own schema at `/v1/schemas/snapshotInput`, not from memory.

## What is not covered by tests

`kubectl` in a Claude session authenticates as a read-only ServiceAccount and `sudo` is denied,
so **nothing in this repo's test suite has ever run this sequence**. The tests pin the order,
the guards, the request bodies and the snapshot selection. Whether the Longhorn API performs the
revert is proven only by the 2026-08-21 drill and by task 6 of this slice, which exercises the
role against a real volume through Ansible.
