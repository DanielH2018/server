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

### D9 — The media volume is a *static* `local` PV, not a dynamic `local-path` PVC *(added by B2, 2026-08-07)*

D6 chose `local-path` for the media. Writing B2 turned up three things dynamic provisioning gets
wrong for this particular volume, none of which contradict D6's reasoning — it is still node-local
and still unreplicated:

1. **The path has to be nameable before the volume exists.** `local-path` names its directory
   `pvc-<uuid>_<ns>_<claim>`. The seeding rsync, this document and an Ansible task all need to say
   where the bytes go, and a UUID minted at bind time cannot be any of those.
2. **`local-path` reclaims `Delete`.** One mistyped `kubectl delete pvc` would take the library with
   it — and `kopiaignore.j2` excludes `containers/data/`, so **the media has no backup**, before or
   after this migration. `Retain` makes the destructive act require two steps instead of one.
3. **`WaitForFirstConsumer` is a chicken-and-egg for a volume that must be filled first.** A dynamic
   claim does not bind, and its directory does not exist, until a pod mounts it. B2's whole purpose
   is to fill the volume *before* any workload exists.

So: a hand-declared `PersistentVolume` of type `local` at **`/srv/media`**, `Retain`, pinned to
daniel-box by `nodeAffinity`, pre-bound by `claimRef`, under a no-provisioner StorageClass
`media-local`. `local` rather than `hostPath` because it is the node-local type that supports
`nodeAffinity` — the scheduler then enforces the pin that D6 accepts, instead of it being left to
hope.

**Access mode is `ReadWriteMany`**, because nine pods mount it simultaneously. That is not a
distributed-filesystem claim: `nodeAffinity` puts all nine on the one node, where the kubelet
bind-mounts the same directory into each. `ReadWriteOnce` would also work — it means one *node*,
not one pod — but would misdescribe the intent.

**The trap this buys**, recorded in the manifest as well: the binding controller stamps the PVC's
UID into `claimRef`. Delete the PVC and the PV goes `Released` holding a UID that no longer exists,
so a same-named PVC will **not** rebind — the data is intact and the volume silently refuses to
mount. Recover with `kubectl patch pv media-data -p '{"spec":{"claimRef":null}}'`.

### D10 — ~~qbittorrent's router forward moves early~~ *(the question was wrong — 2026-08-07)*

**There is no router forward, and there never was.** Asked and answered "move it early"; then
checking the compose before acting on that showed the premise does not hold:

```
docker inspect qbittorrent → NetworkMode=container:<wireguard>   ports=map[]
6881 closed on daniel-server's host
```

qbittorrent publishes **no host port at all**. It runs in the `wireguard` container's network
namespace, behind a Mullvad tunnel with a kill-switch (iptables REJECT for anything not leaving via
`wg0`). Every packet it sends or receives goes through Mullvad. A router forward to daniel-server
would land on a closed port.

So the hazard this decision was answering — "the listen port stops being reachable at cutover" —
describes a setup this homelab does not have. Peering is whatever Mullvad gives it today, and that
is unchanged by which host runs the container. **Nothing to move, nothing to decide, no degradation
to accept.** The real question was never the forward; it is D11.

### D11 — qbittorrent moves as a two-container pod, VPN sidecar and all *(2026-08-07)*

What the plan called "one of the nine" is the slice's most intricate port, and none of it was
visible from the service list:

| Docker | What it needs in k8s |
|---|---|
| `network_mode: service:wireguard` | Two containers in **one pod** — shared netns is native here, and this part is genuinely *easier* than Compose |
| `NET_ADMIN` + `NET_RAW` on wireguard | Container `securityContext.capabilities` — the kill-switch installs rules in the `raw` table |
| `sysctls: src_valid_mark=1`, ipv6 off | **One** pod sysctl + one kubelet allow-list entry — measured, see below |
| `dns: [pi-hole, 1.1.1.1]` | `dnsPolicy: None` + explicit `dnsConfig` — the mod resolves `api.mullvad.net` *before* the tunnel exists |
| Mullvad key/account file-mounted | A k8s Secret, `FILE__MULLVAD_*`, values `\| trim`med (a trailing newline corrupts the key) |
| `depends_on: service_healthy` | No equivalent — needs an init container or a qbittorrent-side wait |

**The trap that is not in that table:** `LAN_NETWORKS=192.168.0.0/24,<lan>,172.16.0.0/12` is the
kill-switch's allow-list. The k3s pod CIDR (`10.42.0.0/16`) and Service CIDR (`10.43.0.0/16`) are in
none of those ranges, so as written the kill-switch would REJECT the return path for Traefik's
requests to the WebUI *and* for the kubelet's probes. The pod would come up, the tunnel would be
healthy, and the container would be unreachable — a failure that reads as a broken Service rather
than a firewall doing its job.

This is why B4 splits: the four remaining \*arrs are near-clones of the sonarr role, while
qbittorrent is a genuinely new shape with its own risk surface.

#### The sysctls, measured rather than assumed (2026-08-07)

A credential-free probe pod — no tunnel, no Mullvad key, just `NET_ADMIN`/`NET_RAW` and the same
capability set the real container would carry:

```
/proc/sys mounted  ro,nosuid,nodev,noexec        <- NET_ADMIN cannot write it
src_valid_mark = 0    rp_filter = 2 (loose)
IPv6: ::1/128 (host) + fe80::…/64 (link)         routes: fe80::/64 dev eth0 only
write to src_valid_mark -> Read-only file system
```

Three things fall out, and two of them shrink the ask:

- **The choice really is binary.** `/proc/sys` is read-only even with `NET_ADMIN`, so the value has
  to arrive from the pod spec (kubelet allow-list) or from a privileged init container. There is no
  third way where the container sets it itself.
- **The two `disable_ipv6` sysctls are not needed.** IPv6 is *enabled* in the pod, but its only
  addresses are loopback and link-local and its only route is `fe80::/64` — nothing can leave the
  link, so there is no leak path for the kill-switch to close. Drop them; do not allow-list them.
- **`src_valid_mark` IS needed, for a reason that is not routing.** `rp_filter` is 2 (loose), so
  strict reverse-path filtering — the thing the sysctl exists for — is not in play. But `wg-quick`
  line 240 reads:

  ```bash
  [[ $proto == -4 ]] && [[ $(sysctl -n net.ipv4.conf.all.src_valid_mark) != 1 ]] && cmd sysctl -q …=1
  ```

  Conditional: it writes **only when the value is not already 1**. At 0 it will attempt the write,
  hit read-only `/proc/sys`, and abort the tunnel bring-up. Pre-set it to 1 and wg-quick skips the
  write entirely and proceeds. So the sysctl is required to stop wg-quick from trying, not to make
  the routing work.

**Decided: one kubelet allow-list entry, no privileged container.**
`--kubelet-arg=allowed-unsafe-sysctls=net.ipv4.conf.all.src_valid_mark` in `k3s_server_args`, plus
`securityContext.sysctls` on the pod. The alternative — a privileged init container — would put a
second privileged component into the media stack that B1 deliberately designed to have exactly one,
and it would put it in the workload handling the fleet's largest untrusted-input surface. "Unsafe"
here is a conservative kubelet label, not a risk ranking: this sysctl is namespaced, so a pod
setting it affects its own netns and nothing else.

**One caveat when backing it out:** `roles/setup/k3s` detects a flag *added* to `k3s_server_args`
but explicitly does not detect one *removed*, so deleting this line later needs a deliberate
reinstall rather than just an edit.

---

## Batches — vertical, each independently exercisable

### B1 done — 2026-08-07 — and it inverted D2 on the way

A throwaway pod running the production Jellyfin image as a non-root user, one
`ffmpeg -init_hw_device vaapi` encode. `-init_hw_device` rather than `-hwaccel` on purpose: it
fails loudly when VAAPI is unavailable instead of silently falling back to software.

**First attempt failed, and that was the point of running it first.** hostPath +
`supplementalGroups` made the device *visible* but not *openable* — `Operation not permitted`,
which is EPERM from the cgroup v2 device filter, not a permissions problem. D2 inverted to a device
plugin as a direct result. Had this been discovered after the 19 G migration it would have been the
expensive ordering.

**Second attempt, with `generic-device-plugin` advertising the render node as `devic.es/dri`:**

```
OPEN OK — the device cgroup now permits this container
vainfo: Driver version: Mesa Gallium driver 25.0.7 for AMD Radeon 780M Graphics (radeonsi, phoenix, ACO)
      VAProfileH264High     : VAEntrypointEncSlice
      VAProfileHEVCMain10   : VAEntrypointEncSlice
      VAProfileAV1Profile0  : VAEntrypointEncSlice
frame=  150 fps=0.0 q=-0.0 time=00:00:04.96 speed=10.9x
```

Five seconds of 1080p30 encoded at **10.9× realtime**, with H.264, HEVC Main10 **and AV1** encode
entrypoints available — AV1 encode being something the outgoing Intel XE part does not offer, so
this is a capability gain rather than only a migration.

**Shipped as `roles/k8s/dri-device-plugin`**, image pinned by digest to exactly what the probe
validated, deploy idempotent at `changed=0`. The role asserts the node is *advertising* the
resource rather than merely that the DaemonSet is Running — without that, the plugin can be healthy
while jellyfin and tdarr sit Pending forever, unschedulable for a resource nothing offers.

**Two things worth carrying into B5/B6:**

- The plugin injects **only** `renderD128`. `card0` is no longer in the container at all, which is
  tighter isolation than the hostPath approach would have given — worth keeping rather than
  "fixing" if something later expects `card0`.
- `Failed to create /root/.cache for shader cache (Permission denied)` — cosmetic, and the encode
  works regardless, but `HOME` is `/root` while the container runs as uid 1000. Point
  `XDG_CACHE_HOME` at a writable path in the real manifests so the shader cache is not disabled on
  every start.

### B2 done — 2026-08-07 — the volume exists and holds the library

Shipped as `roles/k8s/media-volume`: a static `local` PV at `/srv/media` (D9), its no-provisioner
StorageClass, the claim, and a hardlink probe Job. The 19 G copy is `rsync` over ssh, gated behind
`-e media_volume_sync=true` — **never automatic**, because it mirrors with `--delete` and running it
after cutover would erase whatever qbittorrent had downloaded since.

| Measured | |
|---|---|
| Tree | 19 G in **26 files** — few, very large. No hardlinks and no symlinks exist yet. |
| Full copy | **168 s**, ≈121 MB/s — essentially gigabit line rate |
| No-change delta sync | **0.25–0.73 s** across three runs ← *the cutover window starts here* |
| Full-checksum verification | 19 s |
| Hardlink probe | `OK: /data/torrents <-> /data/media are one filesystem` |
| PV / PVC | `media-data` Bound, RWX, Retain, `media-local` |

**The verification is by content, not by timestamp.** The plan said `rsync -n`, which compares size
and mtime and would call a truncated or bit-rotted file identical. It runs `-n -c` instead — a full
read of both sides — for one reason: `kopiaignore.j2` excludes `containers/data/`, so this library
has no backup, and once daniel-server's copy goes there is no second copy to compare against later.
The cost is small and was checked rather than assumed: `-c` takes 18.6 s against 0.5 s for the
metadata-only comparison, a 36× gap with 6.9 s of system time, so it is demonstrably reading the
bytes. That pass is deliberately **excluded from the delta measurement** above, which it would
otherwise inflate by two orders of magnitude.

**Two numbers, not one.** The full copy is a one-off; the delta is what a cutover actually pays,
plus whatever qbittorrent wrote since the last sync — at 121 MB/s that is roughly a second per
100 MB of new downloads.

**The exit test immediately caught two bugs, both mine, both in this role:**

1. The hardlink probe created and deleted a file in `media/` and `torrents/`, which bumped those
   directories' mtimes and left a freshly-synced volume reporting `.d..t...... media/` forever
   after. The probe now saves and restores both directory times, so it leaves no trace.
2. The Ansible task creating the directories set `mode: 0775`, which fought rsync's `-p`: every
   deploy flipped them to 775, every sync flipped them back to 755. Non-idempotent, and it would
   have shown up in the verification as a `.d..p......` phantom difference meaning "the copy is
   wrong". The task now sets ownership only and leaves the mode to the source.

Both were invisible to "the deploy went green" and both were found by running the check twice.

**What B2 does NOT prove.** With one PVC mounted once, `ln` cannot fail — the probe is a regression
guard for the day someone splits torrents and media onto separate volumes, not evidence that the
import path works. That evidence is B3/B4: a hardlink between the root folders sonarr is actually
configured with. What the probe does prove on a first run is real, though — it is the first pod to
mount the claim, so a green run means the static PV binds, the `nodeAffinity` resolves, and uid 1000
can write.

### B3 done — 2026-08-07 — the path footgun does not fire, and three probes lied on the way

`roles/k8s/sonarr`, running a seeded copy of the live config against the B2 media volume while
Docker's sonarr stays authoritative.

**The rehearsal passed on the first deploy, and cleanly:**

```
Root folders resolve inside the pod: /data/media/tv (free space 818 GiB)
14 series, matching daniel-server, every path under /data, and 15.0 GiB of episode
files measured THROUGH the mount
```

**818 GiB is the load-bearing number**, not the 14. daniel-server's root folder reports 334 G free;
818 G is daniel-box's disk. Sonarr is therefore stat'ing the *cluster's* filesystem through the
`/data` mount, which is the thing the \*arr absolute-path footgun would have broken. The 15.0 GiB
of episode files is the same argument from the other side: the pod is reading real files, not an
empty tree that happens to exist at the right path.

**The 14-series match establishes the path contract, NOT database integrity**, and the difference
matters. This config was seeded with `seed_volume_force: false` from a `sonarr.db` that Docker's
sonarr was actively writing — SQLite in WAL mode, copied along with its `-wal` and `-shm`. That
snapshot is torn by construction, and `seed-volume` tolerates it deliberately during coexistence.
A count that matches proves no series was added mid-stream; it does not prove the database is
sound. Integrity is B4's job: a `force=true` re-seed against a **stopped** source, where
`seed-volume`'s sha256 assert is the check that actually covers it.

#### What actually isolates the rehearsal copy — and what does not

This copy holds the **live** database: the real indexers, the real download client, the real
release profiles. Unrestrained it would grab into the live qbittorrent, fail to import (the
completed files are on daniel-server, not in this volume), and then act on that failure — sonarr
removes downloads it has given up on, which would delete a torrent the authoritative copy is
waiting for.

The plan was a deny-all-egress `NetworkPolicy`. **Measured: it does not work on this cluster.** The
policy selected the pod correctly (`netfence=media-rehearsal`, confirmed on the pod and in the
policy's `podSelector`), and a pod carrying that label still reached `10.0.0.161:443` with the
policy in force. Worth flagging loudly because the asymmetry is a trap: the same cluster's
**Ingress** policy, `n8n-broker`, *is* verifiably enforced, and k3s's kube-router netpol controller
(`v2.6.3-k3s1`) is running and logs a clean start. Do not assume an egress policy here does
anything without probing it.

What actually holds is the **name-resolution boundary**. Every outbound target in sonarr's database
is a Docker network name:

```
download client   qBittorrent    wireguard:8080
indexers (x7)     via Prowlarr   http://prowlarr:9696/...
```

None resolve in the cluster — confirmed independently by sonarr's own log, `Unable to retrieve
queue and history items from qBittorrent`. So the unenforced policy was never what protected
anything. It has been **deleted rather than left in place**: an unenforced control that a probe
reports "ok" on is worse than no control, because it reads as protection.

In its place, `isolation-probe-job.yaml.j2` asserts the boundary that does hold, on every deploy.
That is not a formality — **B4–B7 bring prowlarr and qbittorrent into this cluster under exactly
those Service names**, and on that day the names start resolving and a still-rehearsing sonarr
would find them. The probe turns that from a silent change into a failed deploy.

#### Three probes that passed for the wrong reason

Every one of these was green before it was correct, which is the whole reason B3 exists as a
rehearsal:

1. **The fence probe tested a port nothing listens on.** It checked `daniel-server:8989` and
   reported `fence ok`. But the Docker sonarr publishes **no host port** — Traefik reaches it over
   a Docker network — so that connection fails whether a policy exists or not. Fixed by testing
   `:443` (Traefik, genuinely listening) *and* pairing it with an unfenced control job that must
   reach the same target. The control is what exposed the policy as unenforced.
2. **The NetworkPolicy itself.** Covered above — correct in every observable way except effect.
3. **The isolation probe used `nslookup`.** busybox's `nslookup` ignores the `search` list in
   `/etc/resolv.conf`, so a **bare Service name never resolves through it**. Every host came back
   "isolated" unconditionally — including `sonarr`, which demonstrably exists. Fixed by using
   `getent hosts`, which goes through musl's resolver and honours the search path, i.e. asks the
   question sonarr's own HTTP client asks. The guard was then **tested in both directions**: it
   passes on `prowlarr`/`wireguard` and fails the deploy on `sonarr`.

A control that used an FQDN would not have caught #3, and a probe without a control would not have
caught #1 or #2.

### B4a — The three remaining \*arrs, built but not deployed

`radarr`, `prowlarr`, `bazarr` — near-clones of the sonarr role, differing only in port, claim and
(for prowlarr) the absence of a media mount.

**They cannot be deployed incrementally, and the guard is what says so.** Bringing prowlarr up in
the cluster makes the bare name `prowlarr` resolve, which is exactly what sonarr's isolation probe
fails on. That is not an obstacle to work around — it is D3 ("the nine cut over in one window")
being enforced by something executable instead of remembered. So these are written, rendered and
linted, and applied only in the cutover window.

### B4b — qbittorrent and the VPN sidecar

The new shape, not a clone: a two-container pod sharing a netns, `NET_ADMIN`/`NET_RAW`, unsafe
sysctls, a Mullvad Secret, and a kill-switch allow-list that must learn the cluster CIDRs (D11).

**Prove it:** the WebUI answers *through Traefik* (which proves the kill-switch is not eating the
return path), and egress leaves via the tunnel — `wg show wg0` plus a reachability check that can
only have gone through Mullvad, the same two-signal test the Docker healthcheck uses.

### B4c — The cutover window

Stop the Docker five → final delta rsync → re-seed every config with `force=true` against the
stopped source → flip `<svc>_k8s_rehearsal` to false → deploy.

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
- ~~**`qbittorrent` needs its listen port reachable**~~ — **withdrawn 2026-08-07, the premise was
  wrong.** It publishes no host port and runs inside the Mullvad tunnel, so there is no forward to
  move and no peering change at cutover. See D10. What replaces it as the qbittorrent hazard is
  D11's kill-switch allow-list, which does not know the cluster CIDRs.
- **19 G is today's number.** Re-measure before the cutover; the window scales with the delta, not
  the total.
- **`sonarr-k8s.local` is a live Authelia-gated route to a pod holding the real database** (B3).
  The isolation probe asserts only what sonarr can reach *by name*; it says nothing about what a
  human does through the UI. Anyone who logs in can change a root folder, trigger a mass rename,
  or add a download client **by IP** — which would work, because egress is unenforced here. That
  is acceptable for a rehearsal someone is driving deliberately; it is the hazard the probe does
  not cover, and it ends when B4 makes this copy the authoritative one.
- **`/srv/media` is a point-in-time copy taken 2026-08-07 20:14 UTC, and nothing reconciles it.**
  Every byte qbittorrent writes on daniel-server between then and cutover leaves the two trees
  further apart, silently — the B4 delta sync is what closes the gap, and it is the only thing that
  does. Not worth monitoring for a coexistence window measured in days, but do not read a green
  `media-volume` deploy as "the copy is current": an ordinary deploy does not sync at all.

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
- ~~Which device plugin, and what it costs~~ — **answered: `generic-device-plugin`**, one
  privileged DaemonSet holding the privilege in a single auditable place so all nine media
  workloads stay unprivileged. Approved and shipped.
- ~~How long a delta rsync takes once the bulk copy is warm~~ — **measured 2026-08-07: 0.25–0.73 s**
  for a source that has not moved, against 168 s for the full 19 G copy at ≈121 MB/s. The cutover
  window is that delta plus the bytes qbittorrent adds between the last sync and the stop, so it is
  bounded by the download rate, not by the library size. Re-measure at B4; it is cheap.
- ~~Whether `local-path` on daniel-box has an eviction/size policy~~ — **moot as of D9.** The media
  no longer goes through the `local-path` provisioner at all; it is a hand-declared `local` PV whose
  declared capacity is advisory, because a directory on ext4 has no quota behind it. What is left
  is the ordinary one: the library shares daniel-box's 914 G root with everything else, and nothing
  watches that headroom yet. **`local-path` still provisions no volumes on this cluster** — every
  other PVC is Longhorn — so its storage directory does not even exist.
- **The duplicate default StorageClass** (D5) — decide which one keeps the annotation, separately
  from this slice.
- **Why an Egress `NetworkPolicy` is not enforced on this cluster** (found in B3). Ingress policies
  are; the netpol controller is running and healthy. Not chased down, because B3 did not need it —
  but it is a cluster-wide capability gap, not a sonarr one, and the next person to reach for
  egress isolation will assume it works. Probe before relying on it.
