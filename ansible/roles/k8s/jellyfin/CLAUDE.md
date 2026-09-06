# jellyfin — the media server, and the fleet's only GPU workload

Jellyfin, transcoding on the Intel GPU the `dri-device-plugin` role advertises. It reads the
shared `media-data` library and owns its own config volume.

## At a glance
- **Image:** `lscr.io/linuxserver/jellyfin` (`jellyfin_k8s_image`), pinned in lockstep with the
  `targetAbi` of **both** installed plugins — `jellyfin-ani-sync` and Intro Skipper. Raise a
  plugin and the image together, never one alone
  (`ansible/tests/services/test_anisync_pin_matches_server.py` and
  `ansible/tests/services/test_introskipper_install.py` enforce this).
- **Deploy tag:** `--tags "jellyfin"`. `use_authelia: false` — **no auth**, public route.
- **Route:** `jellyfin.<domain>`.
- **Persists:** `jellyfin-config` (`longhorn`, backed up) — the library database, artwork and
  trickplay data. `media-data` (from `k8s/media-volume`) is mounted read-only, twice.
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
- Both installers duplicate rather than share a loop, deliberately — each is pinned by literal
  string assertions in its own test, and a textual guard stops seeing what it guards once the
  thing moves behind an indirection.
