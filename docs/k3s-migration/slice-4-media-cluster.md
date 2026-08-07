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

### D2 — `/dev/dri` reaches the pod by hostPath + `supplementalGroups`, not by a device plugin

**Kubernetes has no `devices:` field.** The Compose `devices: - /dev/dri:/dev/dri` has no direct
translation, and this is the single most mechanical difference in the slice. The options:

| Option | Verdict |
|---|---|
| `hostPath: /dev/dri` + `securityContext.supplementalGroups: [993]` | **Chosen.** Smallest change, no extra component, matches how the repo already thinks about `/dev/dri` |
| A GPU device plugin (Intel/AMD) | Rejected for now — a DaemonSet and a scheduling extension to solve a problem one node and one hostPath already solve |
| `privileged: true` | Rejected outright — it grants the whole device cgroup to serve one render node |

**993 is a host-specific GID and must be treated as one.** It is `render` on daniel-box; nothing
guarantees the same number elsewhere, and daniel-server joining at slice 7 is exactly when a
hardcoded 993 would become wrong. Put it in `host_vars`, not in the manifest.

**Verify rather than assume that a hostPath device node is usable from a non-privileged pod.** The
device cgroup, not the file permissions, is what decides — and this session's read-only kubeconfig
cannot create a pod to test it. It is the first thing B1 must establish.

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

### B1 — Prove VAAPI in a pod, with no data moved

A throwaway pod on daniel-box: hostPath `/dev/dri`, `supplementalGroups: [993]`, a small sample
file, one `ffmpeg -hwaccel vaapi` transcode.

**Prove it:** the transcode completes and `radeontop`/`amdgpu_top` shows the VCN engine busy during
it — not merely that ffmpeg exited 0, which it will do after silently falling back to software.
Record the working device path, GID and driver package.

**If this fails, stop and re-plan.** Everything after it assumes hardware transcode on this node.

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

- **Whether a non-privileged pod can use a hostPath `/dev/dri`** on this node (D2). The device
  cgroup decides, not file permissions. This session's kubeconfig is read-only and could not test
  it. B1's first job.
- **Which VAAPI driver package the Jellyfin and tdarr images ship for AMD**, and whether the
  linuxserver images need a mod equivalent to the Intel one they currently use.
- **How long a delta rsync takes** once the bulk copy is warm (B2). This is the cutover window and
  it should be a measured number before B4 is scheduled.
- **Whether `local-path` on daniel-box has an eviction/size policy** that a growing media library
  would eventually meet.
- **The duplicate default StorageClass** (D5) — decide which one keeps the annotation, separately
  from this slice.
