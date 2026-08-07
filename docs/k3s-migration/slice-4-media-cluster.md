# k3s Slice 4 — The Media Cluster

**Status:** plan, not yet executed. Written 2026-08-07, from measured state.

Design doc §8 gives this slice one line: *"Media cluster (9 services) + iGPU transcode on daniel-box
— a hardware-transcoded playback succeeds."* §5 adds the part that actually shapes it:

> **Media set — moves as one unit (9).** Pinned to daniel-box by the media PV. `jellyfin` and
> `tdarr` additionally take `/dev/dri` — Intel QSV → AMD VAAPI is not a no-op and transcode
> settings need revisiting.

Both halves are right, and both are bigger than they read. This slice differs from 1–3 in a way
worth naming up front: **the previous slices moved services whose state could be recreated. This
one moves a library, and the thing most likely to break is not a service — it is a set of file
paths recorded inside four SQLite databases.**

---

## Baseline — measured 2026-08-07

**The nine, all on daniel-server** (`containers_list`): `qbittorrent`, `sonarr`, `radarr`,
`prowlarr`, `bazarr`, `jellyfin`, `tdarr`, `configarr`, `janitorr`.

| What | Measured |
|---|---|
| Media tree | **19 G** at `containers/data/media` (`books`, `movies`, `tv`, `media`, `leaving-soon`) |
| Download tree | `containers/data/torrents` — **16 K**, effectively empty right now |
| Config dirs | jellyfin 206 M · tdarr 214 M · sonarr 84 M · prowlarr 84 M · radarr 30 M · qbittorrent 16 M · bazarr 3.7 M (**≈640 M total**) |
| daniel-server disk | 455 G, 103 G used, **334 G free** |
| daniel-box disk | 914 G, 37 G used, **839 G free** |

**The transcode hardware changes vendor.**

| | daniel-server (today) | daniel-box (target) |
|---|---|---|
| GPU | Intel XE (`has_igpu: true`) | **AMD Radeon 780M**, Ryzen 7 8845HS (`c6:00.0 … Phoenix3`) |
| Path | Intel QSV | **VAAPI** on Mesa/radeonsi |
| Render node | `/dev/dri/renderD128` | `/dev/dri/renderD128` present, `render` **GID 993** |

`host_vars/daniel-box.yml` already anticipates this — `has_igpu` sits commented out with a note
that it "means pass `/dev/dri` through, which AMD uses for VAAPI too". So the *device* question is
understood. What is not yet addressed is everything downstream of it: Jellyfin's `DOCKER_MODS`
installs **`opencl-intel`**, and its actual transcode settings (which encoder, which device, tone
mapping) live in Jellyfin's own database, not in any file this repo templates.

---

## The seam that shapes this slice

Slice 3's seam was *"does it read something only daniel-server has"*. Here the seam is different
and sharper:

> **Everything that can hardlink to the media must sit on one filesystem, at one in-container path,
> at the same moment.**

That is why §5 says the nine move *as one unit* — it is not a preference. Today `sonarr` mounts
`containers/data` at `/data`, so it sees `/data/torrents` and `/data/media` as one filesystem and
imports by hardlink: instant, no extra space. Split those across two volumes and every import
silently becomes a **copy** — slower, double the space, and with no error to notice.

The second half of the seam is the design doc's named footgun:

> Sonarr, Radarr and Bazarr store **absolute library paths in their databases**. The symptom of
> getting it wrong is a library that appears intact until an import silently fails.

Those paths are *in-container* paths. They survive the move if and only if the pod presents the
same tree at the same mount point. This is trivial to get right and invisible when got wrong.

---

## Decisions

### D1 — Prove VAAPI on daniel-box before moving a single byte

The slice's exit criterion is a hardware-transcoded playback. That is also the only genuinely
**new** capability here — everything else is a relocation of something already working. It is
therefore the first thing to test, not the last, and it can be tested with a throwaway pod and a
sample file, with no data moved and nothing to roll back.

If AMD VAAPI cannot be made to work in a pod on this node, the whole slice needs rethinking (CPU
transcode, or Jellyfin stays on daniel-server until slice 7). Finding that out after a 19 G data
migration would be the expensive ordering.

### D2 — `/dev/dri` needs a device plugin *(INVERTED by B1, 2026-08-07)*

**Kubernetes has no `devices:` field.** The Compose `devices: - /dev/dri:/dev/dri` has no direct
translation, and this is the single most mechanical difference in the slice.

The plan originally chose **hostPath + `supplementalGroups`** as the smallest change, while noting
that the device cgroup — not file permissions — is what actually decides, and that it had to be
verified rather than assumed. B1 verified it, and **it does not work.**

Probe result, running the production Jellyfin image as uid 1000 with `supplementalGroups: [993]`:

```
uid=1000 gid=1000 groups=1000,993
crw-rw---- 1 root 993 226, 128 /dev/dri/renderD128      <- visible, group matches, mode is rw
head: cannot open '/dev/dri/renderD128': Operation not permitted
```

**`Operation not permitted` is EPERM, not EACCES.** A POSIX permission failure would say
"Permission denied". EPERM, with the right group and a readable mode, is the **cgroup v2 eBPF
device filter** refusing the container (`stat -fc %T /sys/fs/cgroup` → `cgroup2fs`; there is no
`devices.list` to read on v2). hostPath bind-mounts the *directory*, so the node appears — but
nothing ever declared the device to the CRI, so it is absent from the container's device allowlist
and every `open()` fails.

This is a property of Kubernetes, not of this cluster, and no amount of GID or permission tuning
changes it.

| Option | Verdict |
|---|---|
| A **device plugin** advertising `/dev/dri` as an extended resource | **Chosen.** The only non-privileged path: the plugin declares the device through the CRI, which is what adds it to the cgroup allowlist. `smarter-device-manager` exposes arbitrary `/dev` nodes and is the usual choice for exactly this; a vendor AMD plugin is the alternative |
| `hostPath` + `supplementalGroups` | **Disproven by B1.** Device visible, open denied |
| `privileged: true` | Rejected — grants the whole device cgroup to serve one render node |

**The GID still matters, just not on its own.** `render` is **993** on daniel-box, and it is
host-specific: nothing guarantees the number elsewhere, and daniel-server joining at slice 7 is
exactly when a hardcoded 993 would become wrong. It belongs in `host_vars`. A device plugin gets
the container past the cgroup; group membership is still what gets it past the file mode.

**Good news from the same probe:** the driver is *not* the problem. The production image already
ships `radeonsi_drv_video.so` in both `/usr/lib/x86_64-linux-gnu/dri` and
`/usr/lib/jellyfin-ffmpeg/lib/dri`, alongside the Intel `iHD`/`i965` ones. So once the device is
reachable, the AMD VAAPI path has the driver it needs — which removes one of the slice's open
questions and narrows the remaining risk to the plugin.

### D3 — The nine cut over in one window, not service by service

The strangler pattern that carried slices 1–3 does not apply to a shared library. There is no
useful intermediate state where `sonarr` writes to daniel-box while `jellyfin` reads daniel-server:
the two trees diverge the moment the first import lands, and reconciling them afterwards is
manual.

So slice 4 has one cutover, and the work is to make that window short and rehearsed: bulk-copy
while everything still runs (repeatable, online), then a short window for stop → final delta sync →
start.

### D4 — The in-container path contract is `/data`, and it is byte-for-byte

Fixed, and the same for every service that writes:

```
/data/torrents      ← qbittorrent's downloads
/data/media/{tv,movies,books,leaving-soon}
```

`jellyfin` keeps its **two** read-only mounts of the media tree (`/data` *and* `/data/media`) —
that duplication is deliberate and documented in its role: janitorr's `leaving-soon` symlink
targets only resolve if `/data/media/*` exists in Jellyfin's namespace too. Reproduce it; do not
tidy it.

Everything writable lands under **one** PV so hardlinks work. `/data/torrents` and `/data/media`
are directories in that volume, not separate volumes.

### D5 — Every PVC names its storage class explicitly

Measured 2026-08-07: **two StorageClasses are both marked default.**

```
NAME                DEFAULT   PROVISIONER
local-path          true      rancher.io/local-path
longhorn            true      driver.longhorn.io
```

A PVC that omits `storageClassName` is therefore non-deterministic — the resolution is
version-dependent and not something to build on. Every existing manifest in this repo already names
its class, so nothing is broken today; this is a trap set for the next PVC written, and slice 4
writes several. Name the class in every one, and separately fix the duplicate default.

### D6 — Media on `local-path`, configs on `longhorn`

- **Media PV → `local-path`.** 19 G and growing, node-local, and replicating a media library across
  a one-node Longhorn cluster buys nothing. This is what pins the nine to daniel-box, exactly as
  §9 accepts.
- **Config PVCs → `longhorn`.** ≈640 M total, and these *are* the state that matters: the \*arr
  databases holding the library paths. They want snapshots and backup.
- **tdarr's cache and Jellyfin's transcode scratch → neither.** Regenerable and write-heavy;
  `emptyDir` or a `longhorn-nobackup` volume, on the same reasoning that keeps `prometheus_data`
  out of Kopia.

### D7 — The DLNA discovery ports are dropped *(resolved 2026-08-07)*

Today Jellyfin binds three ports **to the host LAN IP specifically**, not `0.0.0.0`:

```
{{ server_ip }}:8096      HTTP, direct LAN access
{{ server_ip }}:1900/udp  DLNA/SSDP discovery
{{ server_ip }}:7359/udp  Jellyfin client auto-discovery
```

8096 ports cleanly to a MetalLB `LoadBalancer` on its own VIP. **The two UDP discovery ports do
not** — both are multicast/broadcast protocols, and MetalLB in L2 mode does not carry multicast to
pods.

**Resolved by asking: nobody uses DLNA.** So both UDP ports are simply dropped. Clients reach
Jellyfin by hostname over the route that already exists, and the manifest carries one
`LoadBalancer` for 8096 and nothing else.

This is worth recording as more than a deletion, because it removes the slice's only pressure
toward `hostNetwork: true` — the alternative that would have restored discovery at the cost of the
pod's network isolation. The rejected options are kept here so a future reader does not re-derive
them: `hostNetwork` for Jellyfin, or a discovery shim left on daniel-server until slice 7. Neither
is needed.

### D8 — Jellyfin's transcode settings are database state and must be reconfigured by hand

`DOCKER_MODS=…opencl-intel` and the Intel-specific `FOWNER`/`DAC_OVERRIDE` capability grants come
out of the manifest. But the settings that decide *how* Jellyfin transcodes — hardware acceleration
type, the render device path, tone-mapping — live in its config database, which this repo does not
template. Copying the config PVC therefore carries the **Intel QSV settings into an AMD host**,
where they will fail at playback rather than at startup.

So: migrate the config, then explicitly switch acceleration to VAAPI in the UI, then verify a real
playback. Treat "the pod is Running" as meaning nothing at all here.

---

## Batches — vertical, each independently exercisable

### B1 — Prove VAAPI in a pod, with no data moved *(partially done 2026-08-07)*

A throwaway pod on daniel-box running the production Jellyfin image as a non-root user, one
`ffmpeg -init_hw_device vaapi` encode. `-init_hw_device` rather than `-hwaccel` on purpose: it
fails loudly when VAAPI is unavailable instead of silently falling back to software.

**Done so far — and it inverted D2.** hostPath + `supplementalGroups` gets the device *visible* to
a non-root pod but not *openable*: `Operation not permitted`, which is the cgroup v2 device filter,
not permissions. Also established: the image already ships `radeonsi_drv_video.so`, so the driver
is not a blocker.

**Still to prove, once the device plugin lands:** the encode completes *and* the VCN engine shows
busy — not merely that ffmpeg exited 0.

**If VAAPI still fails with the device reachable, stop and re-plan.** Everything after this assumes
hardware transcode on this node.

### B2 — Media volume and a first bulk copy, online

Create the media PV on `local-path` and rsync 19 G from daniel-server while everything keeps
running. Repeatable and interruptible; nothing cuts over.

**Prove it:** the trees match by `rsync -n` diff, and a hardlink created between `/data/torrents`
and `/data/media` inside the volume **succeeds** — the single check that the import path will not
silently degrade to copying. Record how long a delta sync takes; that number sets the cutover
window.

### B3 — One \*arr, on the real path contract, still not authoritative

Bring up `sonarr` in the cluster against a **copy** of its config PVC and the B2 media volume, with
Docker's sonarr still the live one.

**Prove it:** its root folders resolve, a library scan finds the same series count as the Docker
instance, and no path appears as missing. This is the footgun rehearsal — do it while a wrong
answer costs nothing.

### B4 — The cutover window

Stop the Docker nine → final delta rsync → start the cluster nine (`qbittorrent`, `sonarr`,
`radarr`, `prowlarr`, `bazarr`) with their seeded config PVCs.

**Prove it:** a real import end to end — add a torrent, let it complete, confirm the \*arr imports
it **by hardlink** (link count 2, no space growth) into `/data/media`.

### B5 — Jellyfin, and the slice's exit criterion

Deploy Jellyfin with the media mounts read-only at both paths, `/dev/dri`, and the Intel mods
removed. Reconfigure acceleration to VAAPI (D8).

**Prove it:** a **hardware-transcoded playback succeeds** — a client forcing a bitrate that
requires transcoding, with the VCN engine measurably busy and Jellyfin's own playback info
reporting hardware, not software.

### B6 — tdarr

The heaviest transcode consumer, and the one that will find VAAPI's edges. Same `/dev/dri` wiring
as B5, its cache on a regenerable volume.

**Prove it:** one file transcodes to completion on the AMD path and the output plays.

### B7 — configarr, janitorr, and retiring the Docker copies

These read the others, so they go last. Then stop the nine Docker services.

**Prove it:** configarr's sync reports clean against the cluster \*arrs, janitorr's `leaving-soon`
symlinks resolve **inside Jellyfin's namespace** (the D4 double-mount, verified from Jellyfin, not
from the host), and `monitor-bridge`'s `arr_queue` / `janitorr` / `fake_remux` checks stay green
against the new endpoints.

---

## Hazards

- **The \*arr path footgun (design §8).** Absolute in-container paths in four SQLite databases. Fix
  by pinning the mount path, never by re-scanning. B3 exists to catch this early.
- **Hardlink degradation is silent.** Imports keep working as copies. Check link counts, not
  success.
- **Jellyfin config carries Intel settings onto AMD** (D8). Fails at playback, not at startup.
- **`monitor-bridge` reaches the \*arrs over Docker networks** (`sonarr:8989`, `radarr:7878`,
  `prowlarr`). Those names stop resolving at cutover, exactly as `n8n` did in slice 2 — and the
  same trap applies: point it at the `-k8s` hostname via the ingress VIP, **not** at a bridged
  `*.local` name, which resolves to daniel-server and is unreachable from a container on that host.
- **`qbittorrent` needs its listen port reachable** or torrents go connectable-but-slow. A
  `LoadBalancer` VIP plus the router forward, which is edge config and belongs with slice 6 —
  decide in B4 whether to move the forward early or accept degraded peering meanwhile.
- **19 G is today's number.** Re-measure before the cutover; the window scales with the delta, not
  the total.

---

## Exit criteria

1. A **hardware-transcoded Jellyfin playback succeeds** on daniel-box, verified by GPU engine
   activity and Jellyfin's own playback info — not by the pod being Ready.
2. An end-to-end import completes **by hardlink**, with link count 2 and no space growth.
3. All four \*arr libraries report the same item counts as before the move, with no missing paths.
4. `configarr` syncs clean and `janitorr`'s symlinks resolve inside Jellyfin's namespace.
5. The nine Docker services are stopped, and monitor-bridge's media checks are green against the
   cluster.

---

## Unverified — resolve during execution, not by assuming

- ~~Whether a non-privileged pod can use a hostPath `/dev/dri`~~ — **answered 2026-08-07: no.**
  EPERM from the cgroup v2 device filter. D2 inverted to a device plugin.
- ~~Which VAAPI driver the Jellyfin image ships for AMD~~ — **answered: `radeonsi_drv_video.so` is
  already present.** Still unverified for the **tdarr** image, which is a different base.
- **Which device plugin, and what it costs.** Most run a privileged DaemonSet; the question is
  whether that is an acceptable trade for keeping the *workloads* unprivileged.
- **How long a delta rsync takes** once the bulk copy is warm (B2). This is the cutover window and
  it should be a measured number before B4 is scheduled.
- **Whether `local-path` on daniel-box has an eviction/size policy** that a growing media library
  would eventually meet.
- **The duplicate default StorageClass** (D5) — decide which one keeps the annotation, separately
  from this slice.
