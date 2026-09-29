# valheim (k8s) — Valheim dedicated server

Archived on Docker 2026-01-07 (`6f942bd2`), reactivated 2026-08-13 **straight onto k3s**. The
archived compose role and its reactivation recipe (deleted in #2385) describe a topology that
no longer exists. `k8s/terraria` is the sibling this role copies.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "valheim"`
- **Images:** `ghcr.io/community-valheim-tools/valheim-server` (`valheim_k8s_image`),
  `<k8s_registry_pull_host>/valheim` (`valheim_k8s_mods_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** `valheim-config` (weekly -> B2 (default target)), `valheim-server` (no backup
  (StorageClass longhorn-nobackup))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — two reasons: (1) probe-less — no
  readinessProbe at all; (2) migrating state — Recreate + RWO volume-claim PVC holding worlds
<!-- /generated_from -->

- **Version-pinned**, and **the tag pins the wrapper, not the game** — SteamCMD fetches the
  current Valheim build on every start, so the game runs the newest release either way.
- **Mods:** BepInEx, with 12 plugins pinned in `defaults/main.yml` (2 live, 10 disabled) —
  see *Modding* below. The second image is built in-cluster from `templates/Dockerfile.j2`
  and holds only the plugin DLLs.
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

## Notable
- **The join password lives in SOPS as `valheim_server_pass`.** The compose value it replaced
  is disclosed in a public git log; treat it as burned.
- **The probes read `/proc/net/udp6`,** not `/proc/net/tcp` and not `/proc/net/udp` alone:
  Valheim is UDP and binds v6, so a copied terraria probe can never pass. It is a kernel-side
  bind check, so a Ready pod proves the port is bound and nothing more.
- **`SETGID` is load-bearing, and its absence is silent** — the container's own cron calls
  `initgroups()` for the hourly world backup, and without the capability every tick fails
  behind a healthy-looking log.
- **No Kuma tile, deliberately** — Kuma's port monitor is TCP-only and Valheim is UDP, the
  same reason wg-easy's tunnel has no tile. Pod death surfaces through k3s Workload Health and
  the pod-restart alerting. A port monitor added here would probe a closed TCP port and sit
  permanently red.
- **First rollout is slow** — SteamCMD downloads ~1.8 G before anything binds, and
  `ansible/roles/k8s/valheim/defaults/main.yml:valheim_k8s_rollout_timeout` is the budget both
  the drain's `rollout status --timeout` and `progressDeadlineSeconds` read (#2409).
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
  silent — mods with BepInEx off is a vanilla server that still reports every plugin copied
  into place, and BepInEx on with an empty list is a modded launch path carrying nothing.
- **Verify by the plugin log lines, not by pod Ready** — the startup probe passes identically
  with zero plugins loaded. `grep -i 'Loading \[.*\]'` over the pod log should name one per
  live entry in `valheim_k8s_mods`, and **loading is not working**: a plugin can log itself
  installed and then throw every frame.
- **Enable one mod at a time and read the boot log before adding the next.** The live set is
  two because Valheim 1.0's rolling updates keep breaking the rest, and `UPDATE_CRON` runs at
  its default `*/15` — so the game can update out from under the mods with no repo change and
  no failing check.
- **Ore through portals is a vanilla world modifier, not a mod** — `valheim_k8s_server_args`
  passes `-modifier portals casual`. A modifier applies at launch and is not written into the
  world, so changing that line and redeploying is the whole procedure, both ways.

`docs/valheim-modding.md` has the two directories the initContainer writes, the
`PRE_BEPINEX_CONFIG_HOOK` mkdir the mod set depends on, the three log lines that look like
success, and how to vet a candidate.

## Editing
- Manifests: `templates/*.yaml.j2` · Defaults: `defaults/main.yml`
- Deploy: `./scripts/deploy.sh --tags "valheim"`
