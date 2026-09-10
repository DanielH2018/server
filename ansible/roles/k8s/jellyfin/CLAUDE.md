# jellyfin — the media server, and the fleet's only GPU workload

Jellyfin, transcoding on the Intel GPU the `dri-device-plugin` role advertises. It reads the
shared `media-data` library and owns its own config volume.

## At a glance
- **Image:** `lscr.io/linuxserver/jellyfin` (`jellyfin_k8s_image`), pinned in lockstep with the
  `targetAbi` of **every** installed plugin — `jellyfin-ani-sync`, Intro Skipper, Webhook,
  Merge Versions, Media Cleaner and SSO-Auth. Raise a plugin and the image together,
  never one alone
  (`ansible/tests/services/test_anisync_pin_matches_server.py`,
  `ansible/tests/services/test_introskipper_install.py`,
  `ansible/tests/services/test_webhook_plugin_install.py`,
  `ansible/tests/services/test_mergeversions_install.py`,
  `ansible/tests/services/test_mediacleaner_install.py` and
  `ansible/tests/services/test_sso_auth_install.py` enforce this). **Each addition
  TIGHTENS the pin**: the image cannot move to a new Jellyfin line until every installed plugin
  has a release for that ABI, and #1648 took the set from five to six (Trakt, #1617, was
  installed and removed the same day). The
  constraint is therefore `max` over the declared
  `targetAbi` values, and **the image tag must be at least that** — `10.11.11.0` today, held by
  ani-sync and Intro Skipper, which is why the image sits at `10.11.11`. Do not read the floor
  off this sentence: `test_every_jellyfin_plugin_target_abi_fits_the_image.py` derives it from
  `defaults/main.yml` and covers a plugin added without its own guard. Every plugin the pod
  loads is now either installed by this role or bundled with the image — *Every plugin the pod
  actually loads* below has the census.
- **Deploy tag:** `--tags "jellyfin"`. `use_authelia: false` — **no forward-auth middleware**,
  public route. What authenticates it is Jellyfin's own accounts plus the **SSO-Auth plugin**,
  which this role installs and which authelia carries a `jellyfin` OIDC client for. That is why
  #1648's call was to pin the plugin rather than drop it: dropping it would have taken 2FA off a
  public route.
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
- **Trakt** was installed as the sixth (#1617) and is **removed** — a `remove-trakt` init container sweeps
  it off the PVC on every start, because dropping the installer alone leaves the directory
  Jellyfin loads. *Plugin analyses that outlive their decision* below has why, and where the
  write-back analysis went.
- **SSO-Auth** is the sixth (#1648), and it is what authenticates the public route: the role
  runs `use_authelia: false`, so beyond Jellyfin's own local accounts this plugin is the only
  auth layer, and authelia carries a live `jellyfin` OIDC client (`two_factor`,
  `client_secret_post`, the `DisablePushedAuthorization` asymmetry) pointed at its redirect path.
  That coupling is why #1648's call was to pin it rather than drop it, and
  `test_sso_auth_install.py` asserts the authelia side still exists so neither half can be
  removed alone. It ran as unmanaged dashboard state on the PVC until this pin — the last member
  of that group. Webhook's shape for the manifest; Media Cleaner's for dependencies, since it
  ships two Duende DLLs the OIDC flow cannot complete without. It is also the only one of the
  six whose **manifest name (`SSO Authentication`) differs from the name it loads under
  (`SSO-Auth`)** — the directory takes the manifest name, and the init container's sweep globs
  `SSO*_*` so a directory under either name is collected. **Which OIDC provider it authenticates
  against stays dashboard state** in `configurations/SSO-Auth.xml`, outside every plugin
  directory: the repo owns the install, never what it points at.
- All six installers duplicate rather than share a loop, deliberately — each is pinned by
  literal string assertions in its own test, and a textual guard stops seeing what it guards
  once the thing moves behind an indirection.

## Every plugin the pod actually loads

The installers above are not the whole set — the image bundles several of its own — so read the
live census from the pod's own log, which needs no API key:

```
kubectl -n homelab logs <pod> -c jellyfin | grep 'Loaded plugin:'
```

Read 2026-09-10 (#1569), grouped by who owns the version:

- **Installed by this role**, version-pinned, checksum-pinned and each guarded by its own test
  file: `Ani-Sync 4.4.0.0`, `Intro Skipper 1.10.11.23`, `Webhook 21.0.0.0`, `Merge Versions
  10.11.0.1` (#1616), `Media Cleaner 3.2.0.101109` (#1619) and
  `SSO-Auth 4.0.0.4` (#1648). The last three were added after this census was first read, which
  is why the list is six where the log of 2026-09-10 showed three in this group. Trakt (#1617)
  was installed and removed the same day; `remove-trakt` keeps it off.
- **Bundled with the image**, so they move with `jellyfin_k8s_image` and need no pin here:
  `AudioDB`, `MusicBrainz`, `OMDb`, `Studio Images`, `TMDb` — all `10.11.11.0`, the server
  version.
- **Unmanaged state on the `jellyfin-config` PVC** — **this group is now empty**, and that is
  #1648's outcome rather than an omission. `SSO-Auth 4.0.0.4` was its last member: installed
  through the dashboard, with no pinned version, no checksum and no recorded `targetAbi`, so an
  image bump could silently drop it (Jellyfin's loader rejects a plugin built for a newer server
  without logging a failure). It is now installed by an init container like the five beside it.
  Every plugin the pod loads is therefore described in git — but note that plugin
  CONFIGURATION is not, and cannot be: OIDC provider settings, Media Cleaner's deletion rules,
  and Webhook's destination all live under
  `/config/data/plugins/configurations/` on this PVC. Same shape as bazarr's provider list.

## Plugin analyses that outlive their decision

Recorded here so the next reader does not re-derive them or re-open a settled call.

### Trakt: installed under #1617, removed 2026-09-10

Trakt was the sixth plugin this role installed and was removed at the operator's request the
same day, before the trakt.tv OAuth device authorisation (#1664) was ever completed. Two
things about the removal are not obvious from the diff:

- **Dropping the install container is not a removal.** The install wrote
  `/config/data/plugins/Trakt_30.0.0.0` onto the `jellyfin-config` PVC, Jellyfin scans that
  directory on every start, and the read-only ServiceAccount cannot exec into the pod. An init
  container is the repo's only write path to the PVC, so the uninstall is one too: `remove-trakt`
  sweeps every `Trakt_*` directory and `configurations/Trakt.xml`, idempotently, on every start.
  A dashboard reinstall therefore does not survive a restart, which is the intended state —
  the repo owns which plugins load. `test_trakt_removed.py` pins the sweep and asserts nothing
  reinstalls it.
- **The write-back analysis that gated the install** — a live rating-write HTTP route with no
  automatic caller, and an import task that can clear watch state but ships with no trigger and
  `SkipUnwatchedImportFromTrakt = true` — is in git history at the commit that added this
  section (`git log -S 'SyncFromTraktTask' -- ansible/roles/k8s/jellyfin/CLAUDE.md`). Re-read
  it before reinstalling; it was read from the `v30` tag and a Jellyfin 12 port on `v31`+ could
  restructure any of it.

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
