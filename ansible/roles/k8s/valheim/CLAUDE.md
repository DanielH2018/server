# valheim (k8s) — Valheim dedicated server

Archived on Docker 2026-01-07, reactivated 2026-08-13 **straight onto k3s**. The archived
compose role and its reactivation recipe (deleted in #2385) describe a topology that no
longer exists. `k8s/terraria` is the sibling this role copies.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses `--tags=valheim`
  with exit 5
- **Images:** `ghcr.io/community-valheim-tools/valheim-server` (`valheim_k8s_image`),
  `ghcr.io/danielh2018/valheim` (`valheim_k8s_mods_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** `valheim-config` (weekly -> B2 (default target)), `valheim-server` (no backup
  (StorageClass longhorn-nobackup))
<!-- /generated_from -->

- **Version-pinned**, and **the tag pins the wrapper, not the game** — SteamCMD fetches the
  current Valheim build on every start, so the game runs the newest release either way.
- **Mods:** BepInEx, with 2 plugins pinned in `defaults/main.yml` — see *Modding* below. The second image is built from `templates/Dockerfile.j2` by the
  promote workflow's `images` job, and holds only the plugin DLLs.
- **Host:** daniel-box, by a hard `nodeSelector` — a member of the **VIP unit** with traefik,
  pihole, mosquitto and terraria. The pin and the MetalLB L2Advertisement nodeSelector move
  together or not at all (`roles/setup/k3s/templates/metallb-pool.yaml.j2`).
- **Ports:** UDP 2456 (game) and 2457 (Steam query) via a LoadBalancer Service,
  `externalTrafficPolicy: Local`, pinned to the node IP rather than a MetalLB VIP — the
  router forward has to target a DHCP/ARP-known device.
- **Storage:** two claims on deliberately different backup postures —
  `valheim-config` (`longhorn`, **backed up**) for worlds/lists/prefs, and
  `valheim-server` (`longhorn-nobackup`) for the SteamCMD install. The install claim is 20Gi
  for a game update's transient peak; at 10Gi the 2026-09-17 update hit ENOSPC (#1866), and
  the `# DECIDED:` block at `valheim_k8s_server_size` in `defaults/main.yml` has the numbers.
  The k3s PVC Fullness tile pages while the claim has less than
  `valheim_k8s_server_update_transient_bytes` (3 GiB) free, whatever its percentage reads
  (#1875).
- **Auth:** none possible — a raw UDP game protocol reaches no Traefik, Authelia or CrowdSec
  chain, so the join password is the only access control.
- **Flux applies it, not Ansible** (D5.15, Wave C). Its `containers_list` entry carries
  `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a merged
  change reaches the cluster when `promote` publishes and verifies the next bundle. To give it
  back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of
  the flag leaves two writers on the objects. `k8s_autodeploy: false` still stops Renovate's
  automerge of the wrapper bump. The Deployment is `Recreate`, so a change that rolls it takes
  the server down until the new pod passes its startup probe: 2m16s from pod start to Ready on
  the 2026-10-14 roll, with the install claim already populated. Every roll restarts the
  console that game-stats' `valheim-stats` tails, which closes each open session at the
  restart line.
- **The join-password Secret substitutes from `homelab-cluster-vars`.** `valheim_server_pass`
  is a `${<key>}` placeholder in the published render. Flux substitutes the value from the
  `homelab-cluster-vars` Secret, which it decrypts from `ansible/vars/cluster-vars.sops.env`.
  The Kustomization depends on `cluster-vars`, so a rotated value is live before valheim
  reconciles. A rotation reaches the cluster only through `cluster_vars.py write`, which the
  operator runs after a hand rotation. The renderer stamps a restart checksum on the
  Deployment, so a changed value rolls the pod.
- **Where the tasks went.** `tasks/main.yml` holds two includes, `k8s/image-builder` and
  `k8s/manifests`, and no Flux path runs either. They stay for the handback. The mods build
  became the `images` job (D5.4), which pushes `ghcr.io/danielh2018/valheim:<content tag>`. The
  defaults name `<k8s_registry_pull_host>/valheim`; the renderer rewrites the `mods`
  initContainer's ref to the GHCR one with `imagePullPolicy: IfNotPresent`
  (`scripts/renderer/renderer_lib/images.py:ghcr_refs`). The server image is third-party and
  keeps its own ref. The Kustomization's `wait` replaces the rollout wait.

## Notable
- **The join password lives in SOPS as `valheim_server_pass`.** The value it replaced was
  hardcoded in this public repo, and the plaintext is still in the git log, so treat it as
  burned.
- **The probes read `/proc/net/udp6`,** not `/proc/net/tcp` and not `/proc/net/udp` alone:
  Valheim is UDP and binds v6, so a copied terraria probe can never pass. It is a kernel-side
  bind check, so a Ready pod proves the port is bound and nothing more.
- **`SETGID` is load-bearing, and its absence is silent** — the container's own cron calls
  `initgroups()` for the hourly world backup, and without the capability every tick fails
  behind a healthy-looking log.
- **No Kuma tile, deliberately** — Kuma's port monitor is TCP-only and Valheim is UDP, the
  same reason wg-easy's tunnel has no tile. Pod death surfaces through the `k8s-workloads`
  rules `K8sDeploymentZeroAvailable` and `K8sContainerCrashLooping` instead, and a port monitor added here would sit permanently red on a closed TCP port.
- **First rollout is slow** — SteamCMD downloads ~1.8 G before anything binds, and
  `ansible/roles/k8s/valheim/defaults/main.yml:manifests_rollout_timeout` is the budget that
  Flux's `timeout` and `progressDeadlineSeconds` both read (#2409). It is a literal role
  default because the renderer reads no include var: as an include var, the flip would have
  left Flux at the 600s default. `valheim_k8s_rollout_timeout` names it for the template.
  `/opt/valheim` is a PVC so that download happens once.
- **The hourly world zips go to the NOBACKUP claim** (`BACKUPS_DIRECTORY`). At the image's
  default they sat on the backed-up claim, and every weekly Longhorn backup re-uploaded the
  whole rotation until the storage-cap alert tripped in 2026-09.
- **The live world is `Midgard`, a modded world**; a vanilla server cannot be relied on to
  read it. The previous `Dedicated` world is untouched while another is selected, so reverting
  is `valheim_k8s_world_name` back to it. `valheim-stats` totals are all-time by design.
- 9001 (supervisord) and 2458 (crossplay) are deliberately unpublished, and cloudflare-ddns
  publishes `valheim.<domain>` direct/unproxied — game traffic cannot ride Cloudflare's HTTP
  proxy.

`docs/valheim-modding.md` has each of these in full.

## Modding
`BEPINEX=true` makes the image install BepInExPack beside the vanilla server and run through
it; it is mutually exclusive with `VALHEIM_PLUS`, unused here.

- **The plugins are baked into an image, not downloaded at boot,** so a Thunderstore outage
  cannot bring the server up vanilla with the pod Ready anyway. That image is named `valheim`,
  not `valheim-mods`: `k8s/manifests` keys `k8s_rebuilt_images` on `manifests_service`, and a
  mismatched name pushes an image no pod runs.
- **`valheim_k8s_bepinex` is DERIVED from the mod list, never set by hand.** Either drift is
  silent: mods with BepInEx off is a vanilla server that still reports every plugin copied
  into place, and BepInEx on with an empty list is a modded launch carrying nothing.
- **Verify by the plugin log lines, not by pod Ready** — the startup probe passes identically
  with zero plugins loaded. `grep -i 'Loading \[.*\]'` over the pod log should name one per
  live entry in `valheim_k8s_mods`, and **loading is not working**: a plugin can log itself
  installed and then throw every frame.
- **Enable one mod at a time and read the boot log before adding the next.** The set is two
  because Valheim 1.0's rolling updates broke the rest. The ten that failed were removed
  rather than left commented out. `UPDATE_CRON` runs at its default `*/15`, so the game can
  update out from under the mods with nothing failing.
- **Ore through portals is a vanilla world modifier, not a mod** — `valheim_k8s_server_args`
  passes `-modifier portals casual`. A modifier applies at launch and is not written into the
  world, so changing that line and redeploying is the whole procedure, both ways.

`docs/valheim-modding.md` has the two directories the initContainer writes, why the
deployment sets no `PRE_BEPINEX_CONFIG_HOOK`, and the three log lines that look like success.

## Editing
- Manifests: `templates/*.yaml.j2` · Defaults: `defaults/main.yml`
- Deploy: merge, and `promote` applies it. `./scripts/deploy.sh --tags "valheim"` exits 5.
