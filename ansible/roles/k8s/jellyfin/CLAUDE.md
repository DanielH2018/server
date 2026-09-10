# jellyfin — the media server, and the fleet's only GPU workload

Jellyfin, transcoding on the Intel GPU the `dri-device-plugin` role advertises. It reads the
shared `media-data` library and owns its own config volume.

## At a glance
- **Image:** `lscr.io/linuxserver/jellyfin` (`jellyfin_k8s_image`), pinned in lockstep with the
  `targetAbi` of **all five** installed plugins — `jellyfin-ani-sync`, Intro Skipper, Webhook,
  Merge Versions and Media Cleaner. Raise a plugin and the image together, never one alone
  (`ansible/tests/services/test_anisync_pin_matches_server.py`,
  `ansible/tests/services/test_introskipper_install.py`,
  `ansible/tests/services/test_webhook_plugin_install.py`,
  `ansible/tests/services/test_mergeversions_install.py` and
  `ansible/tests/services/test_mediacleaner_install.py` enforce this). **Each addition
  TIGHTENS the pin**: the image cannot move to a new Jellyfin line until every one of the five
  has a release for that ABI. The constraint is therefore `max` over the five declared
  `targetAbi` values, and **the image tag must be at least that** — `10.11.11.0` today, held by
  ani-sync and Intro Skipper, which is why the image sits at `10.11.11`. Do not read the floor
  off this sentence: `test_every_jellyfin_plugin_target_abi_fits_the_image.py` derives it from
  `defaults/main.yml` and covers a sixth plugin added without its own guard. One more plugin is
  loaded from the
  config PVC and is outside that lockstep — *Every plugin the pod actually loads* below has the
  census.
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
  writes one. Its Renovate manager (added for #1557) is the odd one of the four: Webhook ships
  from `repo.jellyfin.org` rather than a GitHub release, so the manager tracks the
  `jellyfin/jellyfin-plugin-webhook` tags (a bare major, `v21`) and carries the Jellyfin-line
  ceiling in `extractVersionTemplate` — raise that anchor only when the image moves to
  Jellyfin 12.
- **Merge Versions** is the fourth, and it collapses the several files one movie can have —
  1080p beside 2160p, a remux beside a web-dl — into a single library entry whose versions the
  client offers in a picker. Third-party (`danieladov/jellyfin-plugin-mergeversions`) and absent
  from the official `repo.jellyfin.org` manifest, so its `targetAbi`, guid and digest come from
  the plugin's own published manifest (`danieladov/JellyfinPluginManifest`) and release asset.
  Upstream tags the release LINE by Jellyfin version — `10.11.0.1` and `12.0.0` are the two live
  lines — so **the newest release is the wrong one** on a 10.11 image, the same trap Intro
  Skipper's parallel tags and Webhook's major bump record. Its zip carries the DLL alone, so
  this install is **Intro Skipper's shape, not Webhook's**: the init container writes `meta.json`
  from the guid, name and targetAbi. Which libraries it merges is dashboard state on the PVC.
- **Media Cleaner** is the fifth, and the one whose blast radius justifies the pin: it **deletes
  media files** on a schedule, by rule. It ran as unmanaged dashboard state on the PVC until
  #1619, when the operator's call was to keep it and pin it. Webhook's shape — the zip carries
  its own `meta.json`, so nothing writes one. What is unique here is that **the plugin version's
  fourth segment encodes the server ABI**: one upstream release tag (`v3.2.0`) ships three
  per-ABI assets and the published manifest publishes each as its own version (`3.2.0.101007`,
  `3.2.0.101100`, `3.2.0.101109` for `10.10.7`, `10.11.0`, `10.11.9`), where the segment is
  `<major><minor:02d><patch:02d>`. All three are the same plugin release, so **picking the wrong
  asset is silent** — it installs cleanly and never loads.
  `test_mediacleaner_install.py::test_the_version_suffix_decodes_to_the_target_abi` re-derives
  the encoding rather than trusting the three hand-copied values to agree.
  **Which libraries it may delete from, and every rule deciding what goes, stay dashboard state
  on the PVC either way** — plugin configuration under `/config/data/plugins/configurations/`,
  which the repo never touches. Pinning changes what version runs, not what it is pointed at.
  The dashboard's Troubleshooting tab renders a dry-run report of what a real run would delete;
  read it before changing a rule. Same shape as bazarr's provider list.
- All five installers duplicate rather than share a loop, deliberately — each is pinned by
  literal string assertions in its own test, and a textual guard stops seeing what it guards
  once the thing moves behind an indirection.

## Every plugin the pod actually loads

The five installers above are not the whole set, so the lockstep rule at the top of this file
covers only part of what runs. Read the live census from the pod's own log, which needs no API
key:

```
kubectl -n homelab logs <pod> -c jellyfin | grep 'Loaded plugin:'
```

Read 2026-09-10 (#1569), grouped by who owns the version:

- **Installed by this role**, version-pinned, checksum-pinned and each guarded by its own test
  file: `Ani-Sync 4.4.0.0`, `Intro Skipper 1.10.11.23`, `Webhook 21.0.0.0`, `Merge Versions
  10.11.0.1` (#1616) and `Media Cleaner 3.2.0.101109` (#1619). The last two were added after this
  census was first read, which is why the list is five where the log of 2026-09-10 showed three
  in this group.
- **Bundled with the image**, so they move with `jellyfin_k8s_image` and need no pin here:
  `AudioDB`, `MusicBrainz`, `OMDb`, `Studio Images`, `TMDb` — all `10.11.11.0`, the server
  version.
- **Unmanaged state on the `jellyfin-config` PVC**, installed through the dashboard and
  described nowhere else: `SSO-Auth 4.0.0.4`, and that is now the only one. It has no pinned
  version, no checksum and no recorded `targetAbi`, so an image bump can silently drop it —
  Jellyfin's loader rejects a plugin built for a newer server without logging a failure. It
  deletes nothing, which is the whole reason #1619 treated it as the lower-stakes half of the
  same call; whether it gets the same pin is filed separately. Same shape as bazarr's provider
  list: state that lives in a PVC and that git cannot describe.

## Declined and deferred plugins

Recorded here so the next reader does not re-derive the analysis or re-open the decision.

**Trakt (`Jellyfin.Plugin.Trakt`) — deferred, not declined (#1617).** Watch-history sync, the
non-anime counterpart to ani-sync. Two prerequisites are operator-only: a dashboard OAuth
device-authorisation step, which is per-account state on this PVC and cannot be templated, and
the decision below.

*The write-back analysis, read from `jellyfin/jellyfin-plugin-trakt` at master on 2026-09-10.*
ani-sync's justification above is an **absence** — `score` appears nowhere in its AniList
client, so no path can overwrite a rating set by hand. **Trakt has no such absence.** It carries
`POST /sync/ratings` and a `SendItemRating` that posts a 1–10 score to it
(`Trakt/Api/TraktApi.cs`). What holds instead is a narrower claim: *nothing on the plugin's
automatic paths reaches that method.* `SendItemRating` has exactly one caller, the HTTP route
`POST /Trakt/Users/{guid}/Items/{id}/Rate` (`Trakt/Api/TraktController.cs`). Neither scheduled
task nor either event helper calls it — those push only collection membership, watched history,
playback position and the scrobbler's start/pause/stop. So background sync writes no score, but
**a rating-write path is live as soon as the plugin loads**, reachable by anything holding a
Jellyfin API key, and `rating=0` unrates. That is safe *by nobody calling the endpoint*, where
ani-sync is safe *by construction*.

*The larger hazard, which #1617 did not name.* `SyncFromTraktTask` runs the other direction and
writes Jellyfin's own user data from Trakt's, through `SaveUserData(…, UserDataSaveReason.Import)`
— `Played`, `LastPlayedDate`, playback progress. It imports no ratings, but it **clears watch
state**: when Trakt reports an item unwatched and Jellyfin has it played, it sets
`userData.Played = false` (`Trakt/ScheduledTasks/SyncFromTraktTask.cs`). Watch state is exactly
what `jellyfin-config`'s `longhorn` storage class exists to protect — this file's defaults record
that a rescan rebuilds metadata and loses watch state. A wrong or half-populated Trakt account
therefore has a scheduled path to erasing it. Which direction runs is plugin config on the PVC.

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
