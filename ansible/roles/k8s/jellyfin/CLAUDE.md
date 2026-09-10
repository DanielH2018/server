# jellyfin — the media server, and the fleet's only GPU workload

Jellyfin, transcoding on the Intel GPU the `dri-device-plugin` role advertises. It reads the
shared `media-data` library and owns its own config volume.

## At a glance
- **Image:** `lscr.io/linuxserver/jellyfin` (`jellyfin_k8s_image`), pinned in lockstep with the
  `targetAbi` of **all three** installed plugins — `jellyfin-ani-sync`, Intro Skipper and
  Webhook. Raise a plugin and the image together, never one alone
  (`ansible/tests/services/test_anisync_pin_matches_server.py`,
  `ansible/tests/services/test_introskipper_install.py` and
  `ansible/tests/services/test_webhook_plugin_install.py` enforce this).
- **Deploy tag:** `--tags "jellyfin"`. `use_authelia: false` — **no auth**, public route.
- **Route:** `jellyfin.<domain>`.
- **Persists:** `jellyfin-config` (`longhorn`, backed up, 8Gi) — the library database, artwork
  and trickplay data. `media-data` (from `k8s/media-volume`) is mounted read-only, twice.
- **GPU:** requests the `devic.es/dri` extended resource. `verify.yml` proves it is reachable
  from inside the pod as part of every deploy.
- **LAN address:** a MetalLB LoadBalancer Service pinned to a fixed IP (typed into TV/phone
  clients by hand), asserted after apply — a silent MetalLB misconfiguration otherwise lands
  the Service on a different, auto-assigned address with the pod still healthy.

## Notable
- `k8s_autodeploy: true` despite `Recreate` + RWO storage — the pre-apply Longhorn snapshot
  `k8s/volume-snapshot` takes on `jellyfin-config` (and `k8s/volume-revert` can restore) is what
  makes that safe. `media-data` is mounted but **not** covered by that revert.
- SSDP/DLNA discovery is deliberately unsupported: MetalLB's L2 mode does not carry multicast,
  so this pod stays off `hostNetwork`.
- `jellyfin-ani-sync` syncs watch status to AniList but never a score, so it is safe alongside a
  rating set by hand on AniList; its plugin zip is installed by a `python:3.14-alpine` init
  container (the `unzip`/uid-ownership prerequisites `defaults/main.yml` explains).
- **Intro Skipper** is installed by a second init container of the same shape, into the same
  `/config/data/plugins`. Two things differ. Its release line is per *Jellyfin* version — the
  repository tags `10.11/v…` and `12.0/v…` in parallel, so "the latest release" is routinely
  the wrong one — and its zip carries only `IntroSkipper.dll`, so the init container writes
  `meta.json` itself rather than letting Jellyfin invent one from the directory name. It draws
  no UI of its own: it publishes timestamps through Jellyfin's Media Segments API and the
  clients render the skip button, so nothing here writes `/usr/share/jellyfin/web`.
- **Webhook** is the third, installed the same way. It is Jellyfin's own first-party plugin and
  sends playback and library events to an HTTP endpoint; nothing consumes them yet, so what the
  repo owns is the install and the destination is configured in the dashboard. Its release line
  has moved to Jellyfin 12 — 22.0.0.0 declares `targetAbi` 12.0.0.0 — so **the newest Webhook
  is the wrong one** while the image is a 10.11 build, the same shape of trap as Intro
  Skipper's parallel tags. Its zip carries its own `meta.json`, so unlike Intro Skipper nothing
  writes one. It also has **no Renovate manager**: the two siblings each have a custom manager
  in `renovate.json`, and Webhook ships from `repo.jellyfin.org` rather than a GitHub release,
  so this pin ages with no update signal (filed as #1557).
- All three installers duplicate rather than share a loop, deliberately — each is pinned by
  literal string assertions in its own test, and a textual guard stops seeing what it guards
  once the thing moves behind an indirection.

## The snapshot-space cap on `jellyfin-config`
`tasks/main.yml` patches `spec.snapshotMaxSize` on the volume backing `jellyfin-config`, to
`jellyfin_k8s_snapshot_max_size` — 2 x `jellyfin_k8s_size`, which is 16 GiB today. Without it
Longhorn's default is `"0"`, meaning uncapped, and this role takes a pre-apply snapshot on
every deploy, so the backend grows behind a PVC that is itself capped.

**Longhorn picks the number, not you.** It accepts `"0"` or a value no smaller than
`Volume.Spec.Size` x 2, and it raises the value to exactly that when a volume is expanded. So
16 GiB is the smallest legal cap for an 8Gi PVC, and the var is derived rather than written
out — a hardcoded one goes illegal the moment the PVC is raised past half of it, and the
admission webhook then refuses the patch on every jellyfin deploy.

**A reached cap refuses, it does not prune.** Longhorn stops accepting new snapshots until some
are deleted, and `k8s/volume-snapshot` snapshots BEFORE it prunes — so a cap tight enough to be
hit would latch: the snapshot fails, the deploy fails, and the prune that would free headroom
never runs. The weekly Longhorn backup job snapshots the same volume and would fail with it.
Filed as #1560; unreachable at today's headroom, which is the whole reason the cap is loose.
That is why 16 GiB against ~1.9 GiB of live snapshots (2026-09-10) is deliberately loose rather
than tuned to usage, and why `actualSize` was not used to size it — it counts allocated blocks,
including the snapshot chain, so it overshoots what the filesystem holds.

**The pre-apply snapshot stays armed.** `k8s_autodeploy: true` is justified *by* that snapshot
in `k8s_autodeploy_reason`; disarming it means turning auto-deploy off for the fleet's only GPU
workload, which is a much larger trade than bounding backend growth.
