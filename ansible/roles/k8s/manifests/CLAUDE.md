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
`k8s_autodeploy_snapshot_pvcs` (below), and the deferred pair below.

**A caller may defer the APPLY of some of its manifests and keep the render.**
`manifests_deferred_files: [<basename>]` plus `manifests_deferred_dir_name: <dir>` renders,
prunes and **digests** those files here, into
`/etc/rancher/k3s/manifests/<manifests_deferred_dir_name>/`, and applies none of them. The
caller applies that directory itself, in its own order, reading the path from the published
`manifests_deferred_dir` and gating on the published `manifests_deferred_render`. `pihole` is
the one caller: its two Pi-hole Deployments must not change pod template in the same
`kubectl apply` request, because that Recreate-cycles both LAN resolvers at once (#2884). Before
#2899 pihole rendered instance 2 itself, which put those bytes outside `manifests_digest` — so a
change to `deployment-2.yaml.j2` moved no digest and `probe.py releases --stale-only` could
neither clear pihole's path hit by a match nor catch it by a mismatch, for half the workload.
The directory name is reserved the way `<service>-netpol` is, by
`ansible/tests/k8s/test_deferred_manifest_dir_is_reserved.py`.

**A basename with no template in the caller's role falls back to a SHARED one.**
`manifests_shared_defaults` (this role's `defaults/main.yml`) maps a manifest basename to a
template under `ansible/templates/`, and the render task uses it when the caller names the
basename in `manifests_files` and ships no `templates/<basename>.j2` of its own. `service.yaml`
is the one entry: 25 roles' Service templates were byte-identical wrappers around the
`service()` macro, so they were deleted and `ansible/templates/service-default.yaml.j2` renders
for all of them (#2872). It reads the name and port off the role's `containers_list` entry,
plus the optional `service_port_name` and `service_extra_ports` keys there. The caller still
names `service.yaml` in `manifests_files`, which is what keeps the prune keep-set and
`manifests_digest` unchanged. A role whose Service needs anything else writes its own template,
which always wins.

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
  ternary here, the queued kind `roles/k8s/manifests/tasks/drain.yml` runs `rollout status` with, and the
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
  `k8s_pending_rollouts` accumulator, and `roles/k8s/manifests/tasks/drain.yml` — invoked once per batch from
  `ansible/tasks/k8s_batch.yml` — watches every rollout the batch started at once — `max()` per batch instead of `sum()`. 1386s of serial
  waiting across 31 services became a batch wait on 2026-08-15. So a role that returns is a role
  whose manifests were *accepted*, not one whose pods are up.
- **A rebuilt image rolls only if its name matches the role.** `k8s_rebuilt_images` is keyed on
  `manifests_service`, and that holds for six of the seven built images. It does not hold for
  `n8n-runners`, which the `n8n` role builds under its own name and deploys as a second Deployment —
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

Four consequences a green dry run does not cover:

- The **prune task never runs** — the temp dir is fresh, so nothing stale exists in it.
- **A deferred manifest is rendered but never applied.** Under the flag its directory is a
  SUBdirectory of the temp dir, and `kubectl apply -f <dir>/` is not recursive, so the API
  server never sees it. The render is what the digest and `scripts/validate/k8s_manifests.py`
  need; showing a deferred manifest to the API server is the owning role's to arrange, and
  pihole does not, because a dry run must write nothing on the node outside this role
  (#2611/#2614).
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
namespace?, rolled_by_role?}]`), and the record carries them with the same `restart` decision
(#1902).
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

**`rolled_by_role: true` on an entry says this role may not decide its `restart`, and the owner
amends the record afterwards** (#2902). One entry needs it: `pihole-2`, whose own Deployment the
pihole role applies after the stamp has run. The stamp would read `restart: true` for it — the
render changed and the shared apply did not roll it — and then that later apply rolls it by
changing its pod template, stamping no `restartedAt` for `probe.py health` to find, so the gate
would fail a roll that did happen on every image bump. Such an entry is recorded `restart:
false`, kept out of the pod-template fingerprints (which could only read "not rolled" for it),
and carries the marker into `rollouts[]` so a reader can tell a `restart: false` this apply
decided from one it disclaimed. `ansible/roles/k8s/manifests/tasks/rollout_amend.yml` is what
raises it: the owner passes the workload and whether it issued `kubectl rollout restart`, and
that file rewrites the one `rollouts[]` entry. Only a `rollout restart` may claim `restart:
true`, because it is the only roll that stamps a `restartedAt`. `applied_at` is left as the
apply wrote it — the owner's restart is strictly later, so the comparison still holds. A skipped
or failed amend leaves `restart: false`, which is no expectation rather than a false one.

The write is 0644 and keyed by service, so nothing here may run under `k8s_dry_run`: that mode
writes no release record, and an amended one would name a release that does not exist.

**Secret manifests are digested under a host-local key, never plain-hashed.** They are
rendered under `no_log` from decrypted SOPS values. Until 2026-09-26 they were recorded by name
only, because hashing them adds a read path over that output: a task result, a fact, and
anything that later prints either. That left a digest match unable to clear 33 of 58 services,
so the operator approved a digest built to leave no read path (#2574):

- `secret_digest` is a separate field beside `secret_manifests`, in both records.
  `manifests_digest` is unchanged.
- It is HMAC-SHA256 under `manifests_secret_digest_key`, a root-owned 0600 key that
  `ansible/roles/k8s/manifests/files/secret_hmac.py` creates on first use. The key never
  leaves the host and never appears in a record or a log. A plain sha256 of a manifest from a
  known template would let anyone who reads a 0644 record test guesses at a low-entropy secret.
- `secret_hmac.py` receives two paths and prints one hex digest, one `no_log` task per file.
  No task slurps, looks up or `set_fact`s a secret manifest's content, and the key is never on
  an argv, which is why it is not `openssl dgst -hmac`.
  `ansible/tests/k8s/test_secret_digest_reads_no_content.py` holds all of that.
- An unreadable key writes `secret_digest: ''`, which the reader treats as absent.

`ansible/roles/k8s/manifests/tasks/release_digest.yml:DECIDED: HMAC-SHA256 under a host-local key`
carries the decision at the task that computes it. Deleting the key re-keys every future
digest: each service with secret manifests then reads stale until its next deploy.

`manifests` in the record is one entry per rendered file. A deferred file is keyed
`<manifests_deferred_dir_name>/<basename>` rather than by basename alone, because it lives in
another directory and two same-named files would otherwise collide into one entry — a collision
that reads as a match. Adding a deferred file to a role therefore moves that service's
`manifests_digest`, so it reads stale once until its next deploy writes a fresh record, the same
one-redeploy convergence `secret_digest` had.

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
a deploy reads. Both records take both digests from
`ansible/roles/k8s/manifests/tasks/release_digest.yml`, so the two are computed by one code
path. On 2026-09-25 a fleet render on daniel-box reproduced the recorded digest for all 45
services a dry run can reach, in 5m38s. A rendered one-line change to one template moved the
digest, so the comparison can go red.

**The digest is `probe.py releases --stale-only`'s primary answer, and the path rules are its
fallback** (`scripts/diagnostics/probe_lib/releases_render.py:digest_verdict`, #3046). Three
branches: the digests agree and every path hit is dropped, they disagree and the service is
stale for that reason whether or not a path moved, or the record proves nothing and the path
verdict stands. Until #3046 only the first branch existed, so a service the path narrowing read
clean could drift unseen.

A record proves nothing unless its `commit` is origin/master, its tree was clean, and its `host`
is the release record's. A digest also says nothing about secret manifests: a rendered line
added to uptime-kuma's `static-monitors.yaml.j2`, a secret manifest, left its digest unchanged.
So a verdict needs `secret_manifests` empty on both records, which held for 25 of 58 on that
date (#2586), or the same names on both and `secret_digest` present on both (#2574) — a
`secret_digest` that is present and DIFFERENT is drift, and one that is absent keeps the path
verdict, so services with secret manifests stop reading stale one redeploy at a time.

Two verdicts a mismatch does not override. A service inside the reader's grace window keeps
waiting, because the window exists for a landing's own deploy still being in flight and a digest
cannot tell that from drift. And `commit unknown to this checkout` is a doubt about provenance,
which no digest speaks to.

**`ansible/roles/setup/render_records/` is the hourly producer** (#2587). It renders every
service `scripts/deploy_tools/render_targets.py` lists, at the newest origin/master commit
whose CI is green, from its own detached worktree, and pushes a Kuma tile that goes red when
a run leaves any record unrefreshed or the producer stops. It shipped disarmed until #2614
guarded every host write in a role that includes this one (host scripts, crons, node-staged
modules) on `not k8s_dry_run | bool`. Without that guard, an hourly dry run would ship
origin/master's host half ahead of its landing. `render_records_enabled` switches it off.

Every stamped service can be dry-run since #2588. Each role that includes this one guards its
own cluster writes on `k8s_no_mutate`, so no role needs a dry run refused on its behalf: the
`k8s_dry_run_unsupported` list and `deploy.yml`'s assert against it were deleted once empty
(#2876), and `ansible/tests/deploy/test_k8s_dry_run.py` now holds that guard in their place. n8n's image-checksum annotation reads the registry digest a
deploy would stamp, where it used to render `unstaged` under a dry run.

`ansible/roles/k8s/manifests/tasks/release_stamp.yml:DECIDED: this digest names the bytes`
carries the same conclusion at the line that writes the digest.

## The batch drain (`tasks/drain.yml`)

`ansible/tasks/k8s_batch.yml` includes `tasks_from: drain.yml` at the end of every batch. The
drain waits, concurrently, on every rollout the batch's roles queued into
`k8s_pending_rollouts`, then snapshots restart counts for the deferred stabilisation gate.
configarr includes the same file by name to wait for sonarr and radarr before it reconciles.
It lived in its own `rollout-drain` role until #2813 folded it in here.

**Every task in it is `tags: [always]`, not `[deploy]`, and that tagging is load-bearing.**
`[deploy]` tasks are filtered out of every gitops-deploy run. When this logic was tagged
`[deploy]`, it waited on nothing while the play reported `failed=0`. That was measured on a
real `--tags littlelink` deploy: `ok=94, failed=0`, and the drain executed zero times.

`k8s/manifests` used to run `kubectl rollout status` inline, serially, right after its own
apply. Measured over a full deploy, that was 1386s across 31 waits against 435s of actual work.
The waits were serial because Ansible is serial. The cluster did not need them to be: `kubectl
apply` is asynchronous, and k3s reconciles every workload concurrently. The drain runs the same
waits as background shell jobs instead, one per queued rollout, each with its own per-role
timeout.

The drain is not `kubectl wait --for=condition=Available`. Every Deployment here is
single-replica with `maxUnavailable` down to 0, so the OLD pod satisfies `Available` for the
whole rolling update. That wait would return before the new pod is even scheduled. Only
`rollout status` gates on the new ReplicaSet.

The tasks are guarded on an empty queue (`k8s_pending_rollouts | length > 0`). So `--skip-tags
deploy`, which skips the queueing task above, leaves the drain a no-op rather than an error.

An edit to `drain.yml` changes how a deploy runs, never what it applies. `probe.py releases`
therefore exempts this one file from the release-staleness census
(`scripts/diagnostics/probe_lib/releases.py:_is_real_change`); the rest of
`tasks/` is the render and still counts.

## Guards

`ansible/tests/` holds the checks that keep callers honest: `test_manifests_apply_guarded.py`,
`test_k8s_dry_run.py`, `test_k8s_rollout_gate.py`, `test_inline_rollout_gates.py`,
`test_manifests_prune.py` (the `manifests_prune` opt-in — flag rendering, the
Secret/PersistentVolumeClaim ban, the registry pilot's label/kind pairing), and the
`test_k8s_autodeploy_*` set. A role rolling a workload outside this role (`claude-otel`,
`pihole`, `prowlarr` do, deliberately) is covered by the inline-gate test rather than exempted.
