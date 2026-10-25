# freshrss — RSS feed aggregator

FreshRSS with a small nginx feed-cache sidecar. See repo-root `CLAUDE.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=freshrss` with exit 5
- **Images:** `lscr.io/linuxserver/freshrss` (`freshrss_k8s_image`), `nginx`
  (`freshrss_k8s_cache_image`)
- **Route:** `freshrss.<domain>` · `freshrss.local.<domain>`, Authelia one_factor
- **Claim:** `freshrss-config` (weekly -> B2 (default target))
<!-- /generated_from -->

- **The nginx sidecar is the feed cache.**
- **Host: daniel-box (k8s), since 2026-08-05 — slice 2.** This role's ConfigMap embeds
  `files/nginx-feed-cache.conf`. Edit the cache conf / extensions HERE, then merge; `promote`
  applies it.
- **Port:** 80
- **Flux applies it, not Ansible** (D5.9, the first Wave B PR). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. Its
  image-bump PRs therefore draw no `land-wait` and no `probe.py health` run. To give it back to
  Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of the
  flag leaves two writers on the objects. Both dependencies, authelia and traefik, stay
  Ansible-owned, so the Kustomization records them in its `homelab/ansible-owned-deps`
  annotation, not in `dependsOn`. The FreshRSS Deployment is `Recreate`, so a change that
  rolls it takes the service down briefly. The feed cache is a one-replica `RollingUpdate`.
- **The `freshrss-snapshot` Job snapshots `freshrss-config` before each revision applies**
  (D5.2a). The freshrss Kustomization depends on `freshrss-snapshot`, so a revision applies
  only after the snapshot is ready; `docs/flux-deploy-plane.md` lists the Job's checks. A
  failed revision that changes freshrss writes the stateful hold, and `promote` does not reset
  `prod`, because a `:latest` digest swap may already have migrated the config volume. Nothing
  reverts the claim on its own: the operator follows the revert runbook in
  `docs/volume-snapshot-drills.md`. `k8s_autodeploy: true` still lets Renovate automerge the
  digest bump.
- **Where the task went.** `tasks/main.yml` holds one task, the include of `k8s/manifests`,
  and no Flux path runs it. It stays for the handback. Its pre-apply snapshot through
  `k8s/volume-snapshot` became the `freshrss-snapshot` Job, and the Kustomization's `wait`
  replaces the rollout wait. The `freshrss-feed-cache` entry in `manifests_extra_rollouts`
  rolled nginx after a cache-config edit, because nginx rereads `conf.d` only on start. The
  restart checksum the renderer stamps on the feed-cache Deployment replaces it, so an edit
  to `files/nginx-feed-cache.conf` still rolls nginx.

## Notable
- Bundles FreshRSS extensions shipped in `files/`: Karakeep button, Wallabag button,
  ToggleSidebar.
- The nginx sidecar (`files/nginx-feed-cache.conf`) caches a **single hard-coded upstream**
  (`rachelbythebay.com`) — it's a targeted cache for that one feed, NOT a general
  outbound-feed proxy. Adding another cached feed means editing the nginx conf.

## Editing
- Extensions/cache: `files/` (the nginx conf is embedded into this role's ConfigMap)
