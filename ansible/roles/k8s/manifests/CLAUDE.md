# manifests — the shared render → apply → queue cycle for every k3s workload

Utility role, not a workload, and it has **no standalone deploy tag**: callers reach it with
`include_role`, never `--tags manifests`. Nearly every role under `ansible/roles/k8s/` includes
it from its own `tasks/` (`grep -rl k8s/manifests ansible/roles/k8s/*/tasks/` lists them), so a
change here lands on every service at once. It renders a role's templates, applies them,
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
  vars:
    manifests_service: <service>          # also the manifest subdirectory
    manifests_files: [deployment.yaml, service.yaml, ingressroute.yaml]
    manifests_secret_files: [secret.yaml] # rendered 0600 under no_log
    manifests_rollout: <deployment name>  # '' skips the wait entirely
    manifests_rollout_kind: deploy        # or 'daemonset'; default 'deploy'
```

Optional and empty by default: `manifests_extra_rollouts` (`{name, image}` per extra Deployment
this role should roll), `manifests_self_rollouts` (`{name, kind, image?, namespace?,
rolled_by_role?}`, for a role restarting its own workloads afterwards),
`manifests_rollout_timeout` (default `manifests_rollout_timeout_default`, `600s`), `k8s_autodeploy_snapshot_pvcs` (declared in the
caller's own `defaults/main.yml`, snapshotted before the apply), and the deferred pair below.

- **Templates stay in the caller's role**, at `roles/k8s/<service>/templates/<name>.j2`, and the
  `src` is anchored to `playbook_dir`: a relative `src` resolves against this role, and so does a
  `{{ role_path }}` passed through `vars:`.
- **A basename with no template in the caller's role renders from a SHARED default.**
  `manifests_shared_defaults` covers `service.yaml` and `ingressroute.yaml`, read off the role's
  `containers_list` entry; the basename still goes in `manifests_files`, the role's own template
  wins, and dropping `hostname` costs a routed entry its traefik ordering edge.
- **A caller may defer the APPLY of some manifests and keep the render.**
  `manifests_deferred_files` plus `manifests_deferred_dir_name` render, prune and digest them
  into a reserved sibling directory the caller applies itself, gating on
  `manifests_deferred_render`. `pihole` is the only caller.

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
- **Dropping a name from `manifests_files` is only half a retirement.** The staged file goes,
  the **live object keeps serving**, and it needs one hand `kubectl delete` the
  `manifest-prune-check.sh` host cron flags. `manifests_prune: true` plus `manifests_prune_kinds`
  removes the live object too, per role, and each kind named must render the
  `homelab/role: <service>` label itself.
- **The prune owns the whole directory, so nothing else may stage a file there**; an unnamed
  file is deleted on that role's next deploy. Write it to a reserved sibling directory
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
  `checksum/<name>` pod annotation from `ansible/templates/checksum-annotation.yml.j2`.
- **Stale Secret keys are patched out explicitly, because `apply` cannot** — it prunes map keys
  only on objects it has a last-applied baseline for. `verify_secret_keys.yml` reconciles every
  Secret doc in each file, not only the first, and refuses an entry declaring no Secret.
- **A `--dry-run` applies `--dry-run=server` from a fresh tempfile directory**, pruning nothing.

## Release records

`release_stamp.yml` writes `/var/lib/homelab/k8s-releases.d/<service>.json` after each real
apply, one step of history in `<service>.previous.json`; read it with `probe.py releases`. It
records the **rendered bytes** and the `host` that produced them rather than the repo sources,
the `rollouts[]` each workload is owed, and `manifests_digest`/`secret_digest` — the digest
`probe.py releases --stale-only` prefers over its path rules, against the hourly record
`ansible/roles/setup/render_records/` writes. Nothing here runs under `k8s_dry_run`.

## Guards

`grep -rl manifests ansible/tests/k8s ansible/tests/deploy` lists the checks that keep callers
honest: the apply and dry-run guards, the rollout gates, the `manifests_prune` opt-in, the
pruned-directory reservation, the checksum census and the stamp-ordering pair. A role rolling a
workload outside this one (`observability`, `pihole`, `prowlarr`) is covered by the inline-gate
test, not exempted.
