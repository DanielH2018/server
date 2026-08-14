# valheim (k8s) — Valheim dedicated server

Archived on Docker 2026-01-07 (`6f942bd2`), reactivated 2026-08-13 **straight onto k3s**.
It did not go back to Docker: the Docker edge retired at E7 and Phase F is draining
daniel-server, so both the archived compose role (`roles/containers/archive/valheim`) and
`roles/containers/archive/CLAUDE.md`'s four-step reactivation recipe describe a topology
that no longer exists. `k8s/terraria` is the sibling this role copies.

## At a glance
- **Image:** `ghcr.io/community-valheim-tools/valheim-server:1.1.0` — pinned. The upstream
  repo was renamed from `lloesche/valheim-server-docker`; only the new ghcr package
  publishes semver tags (the old one is stuck on `latest`/`dev`), and `1.1.0` and `latest`
  are the same digest today. No rolling-tag exception needed, unlike terraria.
- **Host:** daniel-box, by a hard `nodeSelector` — a member of the **VIP unit** with
  traefik, pihole, mosquitto and terraria. The pin and the MetalLB L2Advertisement
  nodeSelector move together or not at all; see `roles/setup/k3s/templates/metallb-pool.yaml.j2`.
- **Ports:** UDP 2456 (game) + 2457 (Steam query) via
  `Service type: LoadBalancer`, `externalTrafficPolicy: Local`, pinned to the node IP —
  the router forward has to target a DHCP/ARP-known device, so not a MetalLB VIP
- **Storage:** two claims on deliberately different backup postures —
  `valheim-config` (`longhorn`, **backed up**) for worlds/lists/prefs, and
  `valheim-server` (`longhorn-nobackup`) for the SteamCMD install (1.8 G download, 3.8 G on disk)
- **Auth:** none possible — raw UDP game protocol, so no Traefik, no Authelia, no CrowdSec
  HTTP chain. The join password is the only access control.
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`
  (`name: valheim`, no port/hostname) and `defaults/main.yml`

## Notable
- **The password did not carry over.** The Docker compose hardcoded
  `SERVER_PASS: ThisPasswordIsAwesome` until `c3330c5a` swapped it for a variable — but
  this repo is **public**, so the plaintext is still readable in the git log. Treat it as
  disclosed. The live value is a fresh one in SOPS as `valheim_server_pass`.
- **The probes read `/proc/net/udp` AND `/proc/net/udp6`, not `/proc/net/tcp`.** Two traps
  stacked here, both hit on the first boot. Copying terraria's probe verbatim is the first:
  Valheim is UDP, a UDP socket has no LISTEN state and never appears in the TCP table, so
  that check can never pass. The second is that checking `/proc/net/udp` alone still never
  matches — **the server binds v6, so the socket shows up only in `/proc/net/udp6`.** With
  the v4-only check the pod sat un-Ready for 27 minutes with a completely working server
  behind it, and would have been killed once the startup threshold expired. `:0998` is hex
  2456. Like terraria's, it is a kernel-side bind check rather than a connect probe —
  anything that actually spoke to the port would log a join attempt every cycle.
- **`SETGID` is load-bearing, and its absence is silent.** The container runs its own cron
  for the hourly world backup and the Steam update check; cron calls `initgroups()` before
  every job, which needs `CAP_SETGID` even when the target user is already root. With caps
  dropped to `ALL` + `SYS_NICE`, every tick failed with
  `(CRON) error (do_command:initgroups(0) failed: Operation not permitted)` while the rest
  of the log looked perfectly healthy — the backups would simply never have run.
- **No Kuma tile, deliberately.** terraria gets one (`terraria-vip.json`, a TCP port check on
  the node IP), but Kuma's port monitor is TCP-only and Valheim is UDP — the same reason
  wg-easy's tunnel has no tile. Pod death surfaces through k3s Workload Health / the
  pod-restart alerting instead. Do not "fix" this by adding a port monitor; it would probe a
  closed TCP port and be permanently red.
- **First rollout is slow.** An empty install PVC means SteamCMD downloads ~1.8 G before
  anything binds, hence `manifests_rollout_timeout: 900s` and `failureThreshold: 60` on the
  startupProbe. Later boots are a delta check plus world load and clear in under a minute.
- **`/opt/valheim` is a PVC, not an emptyDir**, purely so that download happens once.
- **No `DAC_OVERRIDE`**, unlike terraria: `PUID`/`PGID` default to 0, the seed pod restores
  uid 0 with `tar -p --numeric-owner`, and root writing root-owned files needs no override.
  Add it only if a world save ever fails on the backup step. `SYS_NICE` is kept — Steam's
  threading layer raises its own thread priority and warns on every boot without it.
- **9001 (supervisord) is deliberately unpublished.** The archived compose exposed it; it is
  unauthenticated remote process control inside the container. `SUPERVISOR_HTTP` is off.
- **2458 is unpublished too** — crossplay backend, only bound with `CROSSPLAY=true`.
- The image prunes `/config/backups` at `BACKUPS_MAX_AGE=3` days, so the 385 M of 2025 zips
  that came over in the seed age out on their own; the originals stay on daniel-server.
- cloudflare-ddns publishes `valheim.<domain>` direct/unproxied (game traffic cannot ride
  Cloudflare's HTTP proxy) — in **both** the k8s role and the Docker rollback role.

## The world
`Dedicated`, seeded from `daniel-server:/home/ubuntu/server/containers/valheim/valheim/config`,
last saved 2025-11-22. Nothing was moved — that directory is the rollback.

## Editing
- Manifests: `templates/*.yaml.j2` · Defaults: `defaults/main.yml`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "valheim" -e target=daniel-box`
