# manifests — the shared render → apply → queue cycle for every k3s workload

Utility role, not a workload, and it has **no standalone deploy tag**: callers reach it with
`include_role`, never `--tags manifests`. Nearly every role under `ansible/roles/k8s/` includes
it from its own `tasks/` (`grep -rl k8s/manifests ansible/roles/k8s/*/tasks/` lists them), so a
change here reaches every service. It renders a role's templates, applies them,
reconciles Secret keys, records the release, and **queues** the rollout for someone else to
wait on.

`docs/k8s-manifest-cycle-record.md` carries the measurement, incident or derivation behind every
rule below, what a green `--dry-run` does not cover, and where the rendered manifests land;
read it when you change one.

## The caller's contract

```yaml
- name: Deploy <service>
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:                                   # every one optional; a bare include is the norm
    manifests_rollout: <deployment name>  # default manifests_service; '' skips the wait
    manifests_rollout_kind: deploy        # or 'daemonset'; default 'deploy'
```

**The file lists are derived** (#3662): every top-level `templates/*.yaml.j2` but a
`*-job.yaml`, plus the shared default (below) an entry's `port` or `hostname` earns. A name
saying `secret` renders 0600 under `no_log`. `manifests_exclude_files` drops one, reason at the
include; a passed list wins whole.

Optional and empty by default: `manifests_extra_rollouts` (`{name, image?, restart_on?}` per
extra Deployment to wait on and roll), `manifests_self_rollouts` (`{name, kind, image?, namespace?,
rolled_by_role?}`, for a role restarting its own workloads afterwards),
`manifests_rollout_timeout` (default `manifests_rollout_timeout_default`, `600s`), `k8s_autodeploy_snapshot_pvcs` (declared in the
caller's own `defaults/main.yml`, snapshotted before the apply), `k8s_claims` (below), and the
deferred pair below.

- **A role's PersistentVolumeClaims are data, not templates.** `k8s_claims: [{name, size,
  storage_class, access_modes?, namespace?}]` in the caller's own `defaults/main.yml` renders
  each entry from `ansible/templates/claim-default.yaml.j2` to `claim-<name>.yaml` in the role's
  directory, inside the prune keep-set, the digest and the dry run. A role that snapshots applies
  them before the snapshot; any other gets them from the directory apply, after its
  `00-namespace.yaml`. Under Flux the order reverses: `<tag>` applies the claims after the
  `<tag>-snapshot` Job, so that Job skips a new claim of a snapshot role, one absent live and
  from `<tag>`'s inventory, and `<tag>` then creates it. This role declares no default for it,
  because its own default would outrank the caller's (#3387).

- **Templates stay in the caller's role**, and `src` is anchored to `playbook_dir`: a relative
  `src`, or a `{{ role_path }}` passed through `vars:`, resolves against this role.
- **`manifests_shared_defaults` renders `service.yaml` and `ingressroute.yaml`** off the
  `containers_list` entry for a role with no own template. Dropping `hostname` costs a routed
  entry its route and its traefik ordering edge.
- **A caller may defer the APPLY of some manifests and keep the render.**
  `manifests_deferred_files` plus `manifests_deferred_dir_name` render, prune and digest them
  into a reserved sibling directory the caller applies itself, gating on
  `manifests_deferred_render`. Callers: `pihole`, `headlamp`, `netpol-baseline`.
- **`manifests_shadow_dir` keeps a stubbed dry run's render**, the Ansible half of target slice
  R1's shadow render. The operator passes it with `-e`, beside the stub file, to
  `deploy.sh --dry-run`. Each service then renders into `<dir>/<service>`, owned by `sys_user`,
  and the temporary directory is neither created nor discarded. The role's first task refuses
  it outside a k8s dry run, and while any secret key holds a value other than its stub. That
  assert recomputes the check with `homelab_unstubbed_keys` and reads no flag. Empty by
  default: `ansible/roles/k8s/manifests/defaults/main.yml:manifests_shadow_dir`. Its test is
  `ansible/tests/deploy/test_manifests_shadow_dir.py`, and `docs/homelab-render.md` has the
  operator's view.

## Rules a caller can break

- **`manifests_rollout_kind` takes the literal `deploy` or `daemonset`.** An assert refuses `ds`
  and `DaemonSet`: three consumers match the literal string, one a jsonpath branch in
  `ansible/post_tasks/k8s_stabilise_gate.yml` that then passes vacuously.
- **This role does not wait for the rollout.** It appends to the play-scoped
  `k8s_pending_rollouts`, and `tasks/drain.yml` — included once per batch from
  `ansible/tasks/k8s_batch.yml` — waits on all of them concurrently. A role that returns is one
  whose manifests were *accepted*, not one whose pods are up. Every task in `drain.yml` is
  `tags: [always]`: a gitops-deploy run filters `[deploy]` out, which left the drain waiting
  on nothing behind a `failed=0` recap.
- **Dropping a manifest from the file list leaves the live object serving** only where a
  role passes `manifests_prune: false`. An armed role renders through
  `ansible/templates/role-labelled.yaml.j2`, which labels every document, since the `-l`
  selector also filters the apply. Pruned kinds:
  `ansible/roles/k8s/manifests/defaults/main.yml:manifests_prune_allowlist`. It applies in
  `manifests_prune_namespace` (default `k8s_namespace`) only.
- **The prune owns the whole directory, so nothing else may stage a file there**; a file
  outside the resolved lists is deleted on that role's next deploy. Write it to a reserved sibling directory
  (`<service>-netpol`, `<service>-claims`, `registry-jobs`) no `manifests_service` claims.
- **A restart needs the render AND the apply both changed (#3115), and is skipped where the
  apply rolled the workload itself.** Rendered bytes move on a comment edit `kubectl apply` calls
  `unchanged`, and a second roll of a `Recreate` Deployment deletes the pod the apply just
  created mid-pull. The role fingerprints each target's `.spec.template` around the apply into
  `manifests_rolled_by_apply`, honoured by the shared restarts, pihole's and observability's
  private ones, and `rollouts[].restart`. The `secret` trigger stays on
  `manifests_secret_render` alone.
- **A pod that mounts content outside this cycle needs its own restart trigger.** A ConfigMap a
  template builds with `lookup('file'|'template', ...)` is in the rendered bytes already; one
  staged with `kubectl create configmap --from-file` is not, so that role adds a
  `checksum/<name>` pod annotation from `ansible/templates/checksum-annotation.yml.j2`. For a
  flux-owned role, no Ansible restart runs at all. The renderer stamps its own restart
  annotation (`scripts/renderer/renderer_lib/restart.py:RESTART_ANNOTATION`) on each restart
  target, every `manifests_self_rollouts` entry included, so pihole's and
  observability's own restarts have a Flux equivalent
  (`scripts/renderer/renderer_lib/restart.py:restart_targets`).
- **Stale Secret keys are patched out explicitly, because `apply` cannot** — it prunes map keys
  only on objects it has a last-applied baseline for. `verify_secret_keys.yml` reconciles every
  Secret doc in each file, not only the first, and refuses an entry declaring no Secret.
- **A `--dry-run` applies `--dry-run=server` from a fresh tempfile directory**, pruning nothing.
  Under `manifests_shadow_dir` it applies from the kept shadow directory instead.

## Release records

`release_stamp.yml` writes `/var/lib/homelab/k8s-releases.d/<service>.json` after each real
apply, one step of history in `<service>.previous.json`; read it with `probe.py releases`. It
records the **rendered bytes** and the `host` that produced them rather than the repo sources,
the `rollouts[]` each workload is owed, and `manifests_digest`/`secret_digest` — the digest
`probe.py releases --stale-only` prefers over its path rules, against the hourly record
`ansible/roles/setup/render_records/` writes. Nothing here runs under `k8s_dry_run`.

## Guards

`grep -rl manifests ansible/tests/k8s ansible/tests/deploy` lists the checks that keep callers
honest: the apply and dry-run guards, the rollout gates, the prune opt-outs, the
pruned-directory reservation, the checksum census and the stamp-ordering pair. A role rolling a
workload outside this one (`observability`, `pihole`, `prowlarr`) is covered by the inline-gate
test, not exempted.

**The standalone-value guard runs before every render** (target slice R2). Its first task,
under `no_log`, names each key in this service's row of
`ansible/roles/k8s/manifests/files/standalone_secret_keys.json` whose value is undefined or
would not stay the same string once Flux substitutes it. Its assert then fails the deploy and
prints those names, never a value. Nothing has rendered or applied, so the live objects keep
their old values. A map that is missing or does not parse also fails the first task, with its
error censored. `docs/homelab-render.md` has the remedy and the command that regenerates the map.
