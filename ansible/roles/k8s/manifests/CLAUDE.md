# manifests — the shared render → apply → queue cycle for every k3s workload

Utility role, not a workload. 57 roles under `ansible/roles/k8s/` include it from their own
`tasks/main.yml`, so a change here lands on every service at once. It renders a role's
templates, applies them, reconciles Secret keys, records the release, and **queues** the
rollout for someone else to wait on.

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

Optional, and empty by default: `manifests_extra_rollouts` (below),
`manifests_rollout_timeout` (default `300s`), `k8s_autodeploy_snapshot_pvcs` (below).

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
hosts. `tree_dirty` marks a render no commit reproduces.

**Secret manifests are recorded by name and never hashed.** They are rendered under `no_log`
from decrypted SOPS values, and hashing adds a new read path over that output — a task result,
a fact, and anything that later prints either.

## Guards

`ansible/tests/` holds the checks that keep callers honest: `test_manifests_apply_guarded.py`,
`test_k8s_dry_run.py`, `test_k8s_rollout_gate.py`, `test_inline_rollout_gates.py`,
`test_manifests_prune.py` (the `manifests_prune` opt-in — flag rendering, the
Secret/PersistentVolumeClaim ban, the registry pilot's label/kind pairing), and the
`test_k8s_autodeploy_*` set. A role rolling a workload outside this role (`claude-otel`,
`pihole`, `prowlarr` do, deliberately) is covered by the inline-gate test rather than exempted.
