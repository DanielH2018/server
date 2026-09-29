# Valheim modding, and the boot traps behind the server's rules

`ansible/roles/k8s/valheim/CLAUDE.md` is the role doc, and it keeps the rules: plugins are
baked into an image, `valheim_k8s_bepinex` is derived, and pod Ready proves nothing about
mods. This page is the working-out — how each rule was learned, what a failing mod looks like
in the log, and the traps the first k3s boot turned up. A session reads it when it adds or
debugs a mod (#2989).

## Why the plugins are baked into an image

`templates/Dockerfile.j2` fetches each Thunderstore release, verifies its sha256 and flattens
the DLL out; the `mods` initContainer copies them in. A boot-time download would mean a
Thunderstore outage brings the server up vanilla with the pod Ready either way.

**The built image is named `valheim`, not `valheim-mods`.** `k8s/manifests` keys
`k8s_rebuilt_images` on `manifests_service`, so a mismatched name pushes a new image that no
pod ever runs — the recorded `n8n-runners` failure. It contains no server.

**The initContainer writes two directories, and neither is redundant.**
`/config/bepinex/plugins/homelab` is the image's sanctioned drop point, which a BepInEx or
Valheim update rebuilds the install tree from; `/opt/valheim/bepinex/BepInEx/plugins/homelab`
is the tree the server actually loads, and it is only re-synced during such an update — so a
mod bump alone, which moves neither, would otherwise keep running the old DLLs. Both are
wipe-then-copy into a `homelab/` subdirectory: BepInEx scans recursively, and owning a
subdirectory is what makes a dropped mod actually disappear instead of lingering on the PVC.

**`PRE_BEPINEX_CONFIG_HOOK` is one `mkdir`, and the whole mod set depends on it.** The image
syncs `/config/bepinex/plugins` into the install tree only when that tree already has a
`plugins` directory, and BepInExPack's archive ships none — so on a fresh install the
documented drop-point mechanism silently does nothing. The hook is eval'd inside that function
one line before the sync, with `plugins_path` in scope.

**Mod configs land on the backed-up claim.** The image symlinks the install's `BepInEx/config`
to `/config/bepinex`, so per-mod `.cfg` files sit beside the world.

## Why the live mod set keeps shrinking

Live since 2026-09-13: Server_devcommands 1.113.0 and AchievementEnabler 0.3.2. MouseTweaks
1.0.3 and AAABuildMenu 1.0.1 were live from 2026-09-09 until SteamCMD auto-updated the game
from `l-1.0.7` to `l-1.0.12` and broke both — no newer Thunderstore release exists for either,
so they moved to the disabled block. Eight further mods were requested across 2026-09-09 and
every one fails on `l-1.0.7` or is only needed by one that does; each disabled entry in
`defaults/main.yml` carries the error that disabled it. BepInExPack is not listed — the image
installs it itself.

**`UPDATE_CRON` still runs at its default `*/15`, so the game can update out from under the
mods.** A Valheim release the mods have not caught up with can break them with no repo change
and no failing check. Set `UPDATE_CRON: ""` in the deployment to make updates deliberate.

**Every mod here is client-side too.** Azumatt's use ServerSync, which can refuse a client
whose version differs, so players need the same versions locally.

## Reading the boot log: three things that look like success

**A ServerSync version line is NOT proof a mod works.** The log prints `Sending
AzuCraftyBoxes version 1.8.15 ... to the client` on every join because ServerSync registers
statically, and it kept printing for a plugin whose type initializer had already thrown. That
line was read here as evidence three mods were healthy; all three were dead. Attribute a load
error by reading between consecutive `Loading [...]` lines — the error belongs to the plugin
named above it.

**A clean server-side join is not evidence the client can play.** Upstream
ValheimModding-Jotunn 2.29.2 patches `ZNet.RPC_PeerInfo` to buffer a joining client's
packages, replaying them and calling `socket.VersionMatch()` only once its own
`SynchronizeInitialData` coroutine finishes; on `l-1.0.7` that coroutine throws
`MissingFieldException: ZRoutedRpc.Everybody` on its first send, so the buffer never flushes.
The player spawns and is stuck — unable to move, camera shaking — while the server logs an
ordinary successful connect. ReefTeam's fork 2.29.3 fixes that specific failure (verified: the
`Everybody` reference is absent and the GUID is still `com.jotunn.jotunn`), but the mods
needing Jotunn fail for their own reasons, so the whole group is off. **The tell for the next
one: no `Got character ZDOID` line for a peer that connected.**

**Loading is not working.** Read the log for a `MissingMethodException` after the `Loading
[...]` lines as well, which is how Serverside_Simulations was caught: it logged `Serverside
Simulations installed` and then threw every frame.

## Vetting a candidate mod before adding it

Check its assembly for a `ZRoutedRpc.Everybody` reference, the field `l-1.0.7` removed.
Necessary, NOT sufficient — the Harmony `Undefined target method` failures are invisible to
any string check. Enable one at a time and read the boot log before adding the next.

## Boot traps the first k3s rollout turned up

- **The probes read `/proc/net/udp` AND `/proc/net/udp6`, not `/proc/net/tcp`.** Two traps
  stacked, both hit on the first boot. Copying terraria's probe verbatim is the first: Valheim
  is UDP, a UDP socket has no LISTEN state and never appears in the TCP table, so that check
  can never pass. The second is that checking `/proc/net/udp` alone still never matches — the
  server binds v6, so the socket shows up only in `/proc/net/udp6`. With the v4-only check the
  pod sat un-Ready for 27 minutes with a completely working server behind it, and would have
  been killed once the startup threshold expired. `:0998` is hex 2456. Like terraria's, it is
  a kernel-side bind check rather than a connect probe — anything that actually spoke to the
  port would log a join attempt every cycle.
- **`SETGID` is load-bearing, and its absence is silent.** The container runs its own cron for
  the hourly world backup and the Steam update check; cron calls `initgroups()` before every
  job, which needs `CAP_SETGID` even when the target user is already root. With caps dropped
  to `ALL` plus `SYS_NICE`, every tick failed with `(CRON) error
  (do_command:initgroups(0) failed: Operation not permitted)` while the rest of the log looked
  perfectly healthy — the backups would simply never have run.
- **No `DAC_OVERRIDE`**: `PUID`/`PGID` default to 0, the one-off seed at reactivation restored
  uid 0 with `tar -p --numeric-owner`, and root writing root-owned files needs no override.
  Add it only if a world save ever fails on the backup step. `SYS_NICE` is kept — Steam's
  threading layer raises its own thread priority and warns on every boot without it.
- **First rollout is slow.** An empty install PVC means SteamCMD downloads ~1.8 G before
  anything binds, hence `valheim_k8s_rollout_timeout` and `failureThreshold: 60` on the
  startupProbe. That one default is the whole budget: the drain's `rollout status --timeout`
  and the Deployment's `progressDeadlineSeconds` both read it, because a deadline below the
  budget fails the rollout on ProgressDeadlineExceeded whatever the timeout says (#2409).
  Later boots are a delta check plus world load and clear in under a minute.

## Two ports left unpublished, and why

- **9001 (supervisord)** is unauthenticated remote process control inside the container. The
  archived compose exposed it; `SUPERVISOR_HTTP` is off here.
- **2458** is the crossplay backend, only bound with `CROSSPLAY=true`. The image's README
  warns that mods using RPC want gameport+2 open; no plugin run here has needed it. If a mod's
  sync misbehaves, that port plus a router forward is the first thing to try.

## Where the hourly world zips go

The image's hourly world zips go to `/opt/valheim/backups` on the nobackup claim
(`BACKUPS_DIRECTORY`), pruned at `BACKUPS_MAX_AGE=3` days. At the default `/config/backups`
they sat on the backed-up claim, and since a zip shares no bytes with the previous one every
weekly Longhorn backup re-uploaded the whole three-day rotation — 2.2 G a week, 5.0 G of a
7.6 G B2 bucket by 2026-09-02, which tripped the storage-cap alert. The 385 M of 2025 zips
that came over in the seed aged out under the same rule; the originals stay on daniel-server.
