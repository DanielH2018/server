# manifests — the shared render → apply → queue cycle for every k3s workload

Utility role, not a workload. Nearly every role under `ansible/roles/k8s/` includes it from its
own `tasks/`, so a change here lands on every service at once. For the task files that include
it, run `grep -rl k8s/manifests ansible/roles/k8s/*/tasks/` rather than trusting a count written
here — the count drifted twice. It renders a role's templates, applies them, reconciles Secret
keys, records the release, and **queues** the rollout for someone else to wait on.

Named `manifests` rather than `common` so its ansible-lint variable prefix does not collide
with the Docker-side `roles/containers/common` namespace.

## The caller's contract

```yaml
- name: Deploy <service>
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_service: <service>          # also the manifest subdirectory
    manifests_files: [deployment.yaml, service.yaml, ingressroute.yaml]
    manifests_secret_files: [secret.yaml] # rendered 0600 under no_log
    manifests_rollout: <deployment name>  # '' skips the wait entirely
    manifests_rollout_kind: deploy        # or 'daemonset'; default 'deploy'
```

Optional, and empty by default: `manifests_extra_rollouts` (below), `manifests_self_rollouts`
(below, under the release record; it also feeds the pod-template fingerprints),
`manifests_rollout_timeout` (default `manifests_rollout_timeout_default`, `600s`),
`k8s_autodeploy_snapshot_pvcs` (below).

**Templates stay in the caller's role**, at `roles/k8s/<service>/templates/<name>.j2`. The
`src` is derived from `manifests_service` and anchored to `playbook_dir` on purpose. A relative
`src` resolves against the role that owns the task file — this one — so the caller's templates
are invisible to it, and passing `{{ role_path }}` through `vars:` does not help: `include_role`
vars are templated lazily in the *included* role's context, so `role_path` resolves right back
here and every render fails with `Could not find or access .../k8s/manifests/templates/<name>.j2`.

Manifests land under `/etc/rancher/k3s/manifests/<service>/`, **not**
`/var/lib/rancher/k3s/server/manifests/`. k3s's auto-deploy controller re-applies that second
directory on every restart and takes ownership of what it finds, which fights Ansible for the
same resources.

## What bites from outside this file

- **`manifests_rollout_kind` rejects kubectl's own aliases.** `ds` and `DaemonSet` are refused
  by an assert, because three consumers match the literal string `daemonset`: the apply-output
  ternary here, the queued kind `roles/k8s/rollout-drain` runs `rollout status` with, and the
  jsonpath branch in `ansible/post_tasks/k8s_stabilise_gate.yml`. An alias gets a green deploy
  with the gate reading a Deployment's jsonpath off a DaemonSet — `0 == 0`, passing vacuously.
- **Dropping a name from `manifests_files` is only half a retirement, for the ~63 roles that
  have not armed `manifests_prune`.** `kubectl apply -f <dir>/` sweeps the whole directory, so
  this role deletes the staged file for you — but the **live object keeps serving**. It needs
  one hand `kubectl delete`, which the `manifest-prune-check.sh` host cron flags. Bit for real
  on 2026-08-13: a retired IngressRoute deleted live at 18:51 was re-created by the 18:53
  deploy from its stale staged file.
- **The prune owns the whole directory, so nothing else may stage a file there.** A file
  another role or another task writes into `/etc/rancher/k3s/manifests/<service>/` without the
  caller naming it in `manifests_files`/`manifests_secret_files` is deleted on the next deploy
  of that role — a permanently `changed` prune item on an otherwise idempotent run. Write it to
  a sibling directory instead, the way `headlamp-netpol`, `prowlarr-netpol`,
  `n8n-netpol` (#1668), `registry-jobs` (#1669), `media-volume-probe`,
  `netpol-baseline-probe*`, `build-<image>` (`k8s/image-builder`) and `<service>-claims`
  (`k8s/volume-claim`, #1654) do: a name no role's `manifests_service` claims, so no
  `kubectl apply -f <dir>/` sweeps it either. Those names are a reservation, and
  `ansible/tests/k8s/test_no_role_stages_files_in_a_pruned_manifest_dir.py` enforces it (#1670)
  — one invariant refuses a file staged in a pruned directory, the other refuses a
  `manifests_service` that claims a reserved sibling name. `claude-otel`
  moved its dashboard ConfigMaps out for the same reason and carries an explicit `state:
  absent` for the copies it left behind.
- **`manifests_prune` (#1076) removes the live object too, opt-in per role.** Set
  `manifests_prune: true` and `manifests_prune_kinds: [<group/version/Kind>, ...]` on the
  `include_role` call and the apply gains `--prune -l homelab/role=<service>
  --prune-allowlist=<kinds> -n <namespace>`. Every kind named must have the matching
  `homelab/role: <service>` label rendered in ITS OWN template's `metadata.labels` — an
  unlabeled object is invisible to the selector and cannot be pruned, which is the mechanism's
  entire safety argument (a bad kinds list can only ever touch this role's own labeled
  objects). `roles/k8s/registry` is the only role armed so far; see the `manifests_prune`
  DECIDED comment in `defaults/main.yml` for why arming another role is a deliberate two-step
  (label the templates, then list the kinds) rather than a global flip, and why `Secret` and
  `PersistentVolumeClaim` must never appear in any role's kinds list (guarded by
  `ansible/tests/deploy/test_manifests_prune.py`). An orphan whose staged file was deleted
  BEFORE its role armed this — including the claude-otel-ingest IngressRoute this issue names —
  never receives the label and stays invisible to the selector; it still needs one manual
  `kubectl delete`. `manifest-prune-check.sh` keeps watching every role, armed or not, until a
  real deploy has been seen pruning a real orphan.
- **This role does not wait for the rollout.** It appends to the play-scoped
  `k8s_pending_rollouts` accumulator, and `roles/k8s/rollout-drain` — invoked once per batch from
  `ansible/tasks/k8s_batch.yml` — watches every rollout the batch started at once — `max()` per batch instead of `sum()`. 1386s of serial
  waiting across 31 services became a batch wait on 2026-08-15. So a role that returns is a role
  whose manifests were *accepted*, not one whose pods are up.
- **A rebuilt image rolls only if its name matches the role.** `k8s_rebuilt_images` is keyed on
  `manifests_service`, and that holds for six of the seven built images. It does not hold for
  `n8n-runners`, which `n8n-images` builds under its own name while the `n8n` role deploys it —
  a runners-only rebuild reached the registry and never reached a pod, green throughout. A role
  deploying more than one Deployment names the rest in `manifests_extra_rollouts` as
  `{name, image}` pairs (`freshrss`, `prowlarr`, `karakeep`, `n8n` today), where `image` is the
  `k8s/image-builder` name whose rebuild should roll it.
- **The restart is skipped for a workload the apply itself rolled.** The two `rollout
  restart` tasks fire on `manifests_render is changed`, which an image-pin bump satisfies —
  and the apply already rolls that Deployment, because its pod template changed. The second
  roll costs a spare pod on a `RollingUpdate` Deployment; on a `Recreate` one it deletes the
  pod the apply just created while the kubelet is mid-pull, and the drain waits out the whole
  pull before the next ReplicaSet can appear (#1988: karakeep sat `Terminating` for ten
  minutes and failed the 300s drain). So when the render changed, the role hashes the
  `.spec.template` of every workload a restart could follow this apply for — the primary, every
  `manifests_extra_rollouts` entry and every `manifests_self_rollouts` entry — before and after
  the apply; a target whose hash moved is `manifests_rolled_by_apply[name] == true`, and the
  two shared restart tasks, the private restarts in pihole and claude-otel (#1994) and the
  release record's `rollouts[].restart` all skip it. A self rollout outside `k8s_namespace`
  names its `namespace` on the entry, or the hash reads fail on both sides and it restarts
  twice — claude-otel's six live in `observability`, so its declaration carries it.
  The template, not `.metadata.generation`: generation
  bumps on any spec change (navidrome and terraria template `replicas:`), and a replicas change
  beside a ConfigMap change would otherwise skip the restart the ConfigMap needs. A read that
  fails on either side counts as "not rolled", which restarts — the recoverable direction.
  `manifests_image_changed` is untouched by this: a rebuild behind a mutable tag leaves the
  template byte-identical, so the restart is still the only thing that rolls it.
- **A pod that mounts content outside this cycle needs its own restart trigger.** A
  ConfigMap/Secret a role's template builds with `lookup('file'|'template', ...)` needs
  nothing extra: the lookup's content is IN the rendered manifest, so a content change
  changes `manifests_render`'s bytes and the rollout-restart above fires on its own. A role
  that instead stages its ConfigMap with `kubectl create configmap --from-file` — monitor-
  bridge, autofix-bridge, valheim-stats and terraria-stats, all for a script too
  Jinja-hostile to template (curly-brace exposition strings, PromQL selectors) — applies it
  with its own `kubectl apply --server-side` task, entirely outside `manifests_files`, so
  `manifests_render is changed` never sees it and the pod keeps running the code it started
  with. Those roles add a `checksum/<name>` pod-template annotation instead, rendered by the
  shared `checksum_annotation(name, value=...)` / `checksum_annotation(name, path=...)` macro
  in `ansible/templates/checksum-annotation.yml.j2` (value mode renders an already-computed
  checksum verbatim; path mode hashes one file's raw bytes inline via
  `lookup('file', path, rstrip=False) | hash('sha1')`). n8n's own image-digest pod-roll
  annotation reuses the same macro's value mode too, for a consistent annotation line — the
  macro only standardises how the line renders, not what feeds it.
  `ansible/tests/k8s/test_checksum_annotation_census.py` is the guard: every role that stages
  a ConfigMap this way either carries the annotation or is named in that test's `DEBT` dict
  with the reason it does not need one (claude-otel's dashboards poll on their own, per
  Grafana's `updateIntervalSeconds`).
- **Stale Secret keys are patched out explicitly, because `apply` cannot.** `kubectl apply` only
  prunes map keys on objects it has a last-applied baseline for, so one historical
  `kubectl create`/`replace` breaks pruning silently and forever — removing a key from a template
  then deploys green while the live Secret keeps it. `kubectl replace` is not the fix; replace
  strips the annotation and reopens the crack on every use. `verify_secret_keys.yml` reconciles
  **every** Secret doc in each file, not just the first (crowdsec's `config-secret.yaml.j2`
  carries two), and refuses a `manifests_secret_files` entry that declares no Secret at all.
- **Pre-deploy snapshots are opt-in from the caller's own defaults.** A role declares
  `k8s_autodeploy_snapshot_pvcs` in its `defaults/main.yml` and nothing is plumbed through the
  include — role defaults are in scope for an included role. 13 roles opt in; the rest evaluate
  `| default([]) | length == 0` and never make the call. The snapshot is taken **before** the
  apply, since the apply is what starts the pod that migrates the on-disk format.

## `--dry-run` renders somewhere else, and that costs coverage

Under `k8s_dry_run` the role renders into a fresh tempfile directory and applies it with
`--dry-run=server`, so the manifests are judged by the same schema validation, defaulting and
admission a real apply goes through. `--check` cannot substitute: it skips the template writes,
leaving nothing on disk to apply.

Three consequences a green dry run does not cover:

- The **prune task never runs** — the temp dir is fresh, so nothing stale exists in it.
- **Mode and owner differ** from the real path. Neither is something `kubectl apply` judges.
- **`changed_when` is pinned false**, because dry-run stdout carries the same
  `created`/`configured` words a real apply does. The batch drain and the stabilisation gate
  both key on `changed`, so an honest `changed` here would cascade into them.

The `k8s_dry_run` guard on the rollout-restart is explicit rather than falling out of the change
conditions: a dry run renders to a fresh dir every time, so `manifests_render` is always
`changed` and the live Deployment would be restarted on every dry run.

## Release records

`release_stamp.yml` writes `/var/lib/homelab/k8s-releases.d/<service>.json` after each real
apply, keeping exactly one step of history in `<service>.previous.json`. Read it with
`uv run python scripts/diagnostics/probe.py releases`.

It records the **rendered bytes**, not the repo sources — a twelve-factor release is build plus
config, and on this plane the config is the per-host Ansible variables that only exist after
rendering. Two commits can render identical manifests; one commit can render differently on two
hosts. `tree_dirty` marks a render no commit reproduces, and `host` names the inventory host
whose `host_vars` layered into the render (#2532) — the record is only self-describing on
another node, or against a fresh render, if it says which host produced the bytes. Records
written before that field exist carry no `host`; a reader must treat its absence as unknown,
since each service gains the field on its next deploy.

`rollouts` names each workload the shared restart tasks would target — the primary
`manifests_rollout` and every `manifests_extra_rollouts` entry — with `restart: true` where this
apply queued one (a changed render, a changed secret render, or a rebuilt image, and not a
workload the apply created or one the apply itself rolled — `manifests_rolled_by_apply`). `probe.py health` reads it and fails a `restart: true` workload
whose `restartedAt` is not newer than `applied_at` (#1867). The stamp is included before the
restart tasks for that comparison to hold, and after the rebuilt-image fact so both read one
answer; `ansible/tests/k8s/test_release_stamp_rollout_expectation.py` pins the order.

A role that sets `manifests_rollout: ''` and restarts its workloads through a private task
after this role returns declares them in `manifests_self_rollouts` (`[{name, kind, image?,
namespace?}]`), and the record carries them with the same `restart` decision (#1902).
claude-otel passes `claude_otel_stabilise_workloads` with `namespace:
k8s_observability_namespace` on each entry; pihole names both instances with `image: pihole`,
since `roll_one.yml` also fires on `manifests_image_changed`, which keys on the service name.
The entry reaches `rollouts[]` and the pod-template fingerprints (#1994, so the private restart
can read `manifests_rolled_by_apply` for its own workloads): the shared restart and the batch
drain never read it, which is what those roles opted out of. Appending from the private task
itself cannot work, because the record is written before that task runs.
`ansible/tests/k8s/test_self_rollouts_follow_the_apply.py` holds each role's declaration equal
to the loop its private restart iterates, and holds each private restart to the
`manifests_rolled_by_apply` skip.

**Secret manifests are recorded by name and never hashed.** They are rendered under `no_log`
from decrypted SOPS values, and hashing adds a new read path over that output — a task result,
a fact, and anything that later prints either.

**`manifests_digest` identifies the applied bytes, and a render-mode dry run reproduces it.**
That matters because `probe.py releases --stale-only` decides staleness from paths and diffs,
and each of its five narrowings exists because a path moved while the rendered bytes did not.
Comparing the recorded digest against a fresh render would answer all five at once, which is
what issue #2505 asks for.

The offline harness cannot supply that render. Measured on 2026-09-25 against the 57 live
records, `scripts/validate/k8s_manifests.py` reproduced every recorded file checksum for 9
services and mismatched at least one file for 48. It stubs SOPS values, which reach 49 of the
300 rendered manifests as the literal `STUB`. It supplies its own placeholder `domain`
(`example.com`, from `scripts/lib/render_guard.py`), which every `ingressroute.yaml` embeds.
Its role defaults also outrank the inventory, a deliberate inversion its own `DECIDED` marker
explains.

A dry run on the deploy host can (#2574). With `-e manifests_render_record=true`,
`ansible/roles/k8s/manifests/tasks/render_record.yml` digests the throwaway render and writes
`/var/lib/homelab/k8s-renders.d/<service>.json`. It reads the same vars and decrypted secrets
a deploy reads, and it adds no read path over `no_log` output: both records take their digest
from `ansible/roles/k8s/manifests/tasks/release_digest.yml`, which stats `manifests_files`
only. On 2026-09-25 a fleet render on daniel-box reproduced the recorded digest for all 45
services a dry run can reach, in 5m38s. A rendered one-line change to one template moved the
digest, so the comparison can go red.

`probe.py releases --stale-only` reads those records
(`scripts/diagnostics/probe_lib/releases_render.py:render_proves_current`). A matching digest
clears a service's path hits only when the render record's `commit` is origin/master, its tree
was clean, and its `host` is the release record's. A digest match says nothing about secret
manifests: a rendered line added to uptime-kuma's `static-monitors.yaml.j2`, a secret manifest,
left its digest unchanged. So a match also needs `secret_manifests` empty on both records,
which held for 25 of 58 on that date (#2586).

**`ansible/roles/setup/render_records/` is the hourly producer** (#2587). It renders every
service `scripts/deploy_tools/render_targets.py` lists, at the newest origin/master commit
whose CI is green, from its own detached worktree, and pushes a Kuma tile that goes red when
a run leaves any record unrefreshed or the producer stops. It shipped disarmed until #2614
guarded every host write in a role that includes this one (host scripts, crons, node-staged
modules) on `not k8s_dry_run | bool`. Without that guard, an hourly dry run would ship
origin/master's host half ahead of its landing. `render_records_enabled` switches it off.

Every stamped service can be dry-run since #2588. Each role that includes this one guards its
own cluster writes on `k8s_no_mutate`, so `k8s_dry_run_unsupported` holds only `n8n-images`,
which renders no manifests. n8n's image-checksum annotation reads the registry digest a deploy
would stamp, where it used to render `unstaged` under a dry run.

`ansible/roles/k8s/manifests/tasks/release_stamp.yml:DECIDED: this digest names the bytes`
carries the same conclusion at the line that writes the digest.

## Guards

`ansible/tests/` holds the checks that keep callers honest: `test_manifests_apply_guarded.py`,
`test_k8s_dry_run.py`, `test_k8s_rollout_gate.py`, `test_inline_rollout_gates.py`,
`test_manifests_prune.py` (the `manifests_prune` opt-in — flag rendering, the
Secret/PersistentVolumeClaim ban, the registry pilot's label/kind pairing), and the
`test_k8s_autodeploy_*` set. A role rolling a workload outside this role (`claude-otel`,
`pihole`, `prowlarr` do, deliberately) is covered by the inline-gate test rather than exempted.
