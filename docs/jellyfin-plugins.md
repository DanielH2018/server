# Jellyfin plugins — the per-plugin record and the settled decisions

Working-out moved off `ansible/roles/k8s/jellyfin/CLAUDE.md` (#2991), which a session reads on every
touch of the media server. The role doc keeps the rules; this page keeps what each installed plugin
is, what blocks the Jellyfin 12 image line, the live census of what the pod loads, and the two
plugin decisions that outlived their issues.

## What blocks the Jellyfin 12 image line

Ask this before planning an image bump. Four of the five installed plugins have a 12 build ready
(read 2026-09-10): ani-sync `4.6.0.0` in the `v4.6b` release, Intro Skipper `12.0/v12.0.3.0`,
Webhook `22.0.0.0` declaring `targetAbi 12.0.0.0` in the official manifest, and Merge Versions
`12.0.0`.

**Media Cleaner was the blocker and no longer is.** `v3.2.0` shipped only 10.x assets, `v3.4.0`
(#2720) added `MediaCleaner-12.0.zip`, and `v3.7.0` — pinned 2026-09-28 (#2905) — publishes that
asset as `3.7.0.120000` with `targetAbi 12.0.0.0`. A move to 12 must switch this pin to that asset
in the same PR: leaving it on the 10.11.9 asset stops the automated cleanup rules silently, because
Jellyfin's loader rejects an ABI-mismatched plugin **without logging a failure**, so the pod stays
healthy and the rollout stays green (the #1648 silence).

SSO-Auth was the other blocker and is gone; the section below has that decision.

## The five installed plugins

- **`jellyfin-ani-sync`** syncs watch status to AniList but never a score, so it is safe alongside a
  rating set by hand on AniList. Its zip is installed by a `python:3.14-alpine` init container —
  `defaults/main.yml` explains the `unzip`/uid-ownership prerequisites.
- **Intro Skipper** is a second init container of the same shape, into the same
  `/config/data/plugins`. Two things differ: its release line is per *Jellyfin* version — the
  repository tags `10.11/v…` and `12.0/v…` in parallel, so "the latest release" is routinely the
  wrong one — and its zip carries only `IntroSkipper.dll`, so the init container writes `meta.json`
  itself rather than letting Jellyfin invent one from the directory name. It draws no UI of its own:
  it publishes timestamps through Jellyfin's Media Segments API and the clients render the skip
  button, so nothing here writes `/usr/share/jellyfin/web`.
- **Webhook** is Jellyfin's own first-party plugin, sending playback and library events to an HTTP
  endpoint. Nothing consumes them yet, so the repo owns the install and the destination is dashboard
  state. Its release line has moved to Jellyfin 12 — 22.0.0.0 declares `targetAbi 12.0.0.0` — so the
  newest Webhook is the wrong one while the image is a 10.11 build. Its zip carries its own
  `meta.json`, so unlike Intro Skipper nothing writes one. Its Renovate manager (added for #1557) is
  the odd one out: Webhook ships from `repo.jellyfin.org` rather than a GitHub release, so the
  manager tracks the `jellyfin/jellyfin-plugin-webhook` tags (a bare major, `v21`) and carries the
  Jellyfin-line ceiling in `extractVersionTemplate` — raise that anchor only when the image moves to
  Jellyfin 12.
- **Merge Versions** collapses the several files one movie can have — 1080p beside 2160p, a remux
  beside a web-dl — into a single library entry whose versions the client offers in a picker. It is
  third-party (`danieladov/jellyfin-plugin-mergeversions`) and absent from the official
  `repo.jellyfin.org` manifest, so its `targetAbi`, `guid` and digest come from the plugin's own
  published manifest (`danieladov/JellyfinPluginManifest`) and release asset. Upstream tags the
  release LINE by Jellyfin version — `10.11.0.1` and `12.0.0` are the two live lines — so the newest
  release is the wrong one on a 10.11 image. Its zip carries the DLL alone, so this install is Intro
  Skipper's shape, not Webhook's. Which libraries it merges is dashboard state on the PVC.
- **Media Cleaner** is the one whose blast radius justifies the pin: it **deletes media files** on a
  schedule, by rule. It ran as unmanaged dashboard state on the PVC until #1619, when the operator's
  call was to keep it and pin it. It is Webhook's shape — the zip carries its own `meta.json`. What
  is unique is that **the plugin version's fourth segment encodes the server ABI**: one upstream
  release tag (`v3.2.0`) ships three per-ABI assets and the published manifest publishes each as its
  own version (`3.2.0.101007`, `3.2.0.101100`, `3.2.0.101109` for `10.10.7`, `10.11.0`, `10.11.9`),
  where the segment is `<major><minor:02d><patch:02d>`. All three are the same plugin release, so
  picking the wrong asset is silent — it installs cleanly and never loads.
  `test_mediacleaner_install.py::test_the_version_suffix_decodes_to_the_target_abi` re-derives the
  encoding rather than trusting the three hand-copied values to agree. The pin moved to
  `3.7.0.101109` on 2026-09-28 (#2905): `Update Plugins` had already fetched that build unpinned,
  and pinning UP to it beat downgrading a media-deleting plugin onto the `configurations/` XML 3.7
  last wrote.

The dashboard's Troubleshooting tab renders a dry-run report of what a real Media Cleaner run would
delete; read it before changing a rule.

## Why the five installers are duplicated rather than looped

Each installer is pinned by literal string assertions in its own test file, and a textual guard stops
seeing what it guards once the thing it reads moves behind an indirection. The duplication is what
keeps five independent guards honest, so fold them into a loop only by replacing those assertions
with something that still fails on a wrong pin.

## Every plugin the pod actually loads

The installers are not the whole set — the image bundles several of its own — so read the live census
from the pod's own log, which needs no API key:

```
kubectl -n homelab logs <pod> -c jellyfin | grep 'Loaded plugin:'
```

Read 2026-09-28 (#2873), grouped by who owns the version:

- **Installed by this role**, each guarded by its own test file: `Ani-Sync 4.4.0.0`,
  `Intro Skipper 1.10.11.24`, `Webhook 21.0.0.0`, `Merge Versions 10.11.0.1` (#1616) and
  `Media Cleaner 3.7.0.101109` (#1619, pinned at that version by #2905). Two plugins were installed
  and removed soon after — Trakt (#1617) and SSO-Auth (#1648, removed #1674); `sweep-unlisted-plugins`
  keeps both off.
- **Bundled with the image**, so they move with `jellyfin_k8s_image` and need no pin here: `AudioDB`,
  `MusicBrainz`, `OMDb`, `Studio Images`, `TMDb` — all `10.11.11.0`, the server version.
- **Unmanaged state on the `jellyfin-config` PVC** — **this group is empty**, and that is #1648's
  outcome rather than an omission. `SSO-Auth 4.0.0.4` was its last member: installed through the
  dashboard, with no pinned version, no checksum and no recorded `targetAbi`, so an image bump could
  silently drop it. #1648 brought it under an init container and #1674 then removed the plugin
  outright. Every plugin the pod loads is therefore named in git, at the version git names.

Plugin CONFIGURATION is not in git and cannot be: Media Cleaner's deletion rules and Webhook's
destination live under `/config/data/plugins/configurations/` on this PVC. Same shape as bazarr's
provider list.

## Plugin analyses that outlive their decision

Recorded so the next reader does not re-derive them or re-open a settled call.

### Trakt: installed under #1617, removed 2026-09-10

Trakt was the sixth plugin this role installed and was removed at the operator's request the same
day, before the `trakt.tv` OAuth device authorisation (#1664) was ever completed. Two things about the
removal are not obvious from the diff:

- **Dropping the install container is not a removal.** The install wrote
  `/config/data/plugins/Trakt_30.0.0.0` onto the `jellyfin-config` PVC, Jellyfin scans that directory
  on every start, and the read-only ServiceAccount cannot exec into the pod. An init container is the
  repo's only write path to the PVC, so the uninstall is one too. A `remove-trakt` container swept
  `Trakt_*` and `configurations/Trakt.xml` on every start until #2873 folded it into
  `sweep-unlisted-plugins`. A dashboard reinstall therefore does not survive a restart, which is the
  intended state — the repo owns which plugins load.
- **The write-back analysis that gated the install** — a live rating-write HTTP route with no
  automatic caller, and an import task that can clear watch state but ships with no trigger and
  `SkipUnwatchedImportFromTrakt = true` — is in git history at the commit that added that section
  (`git log -S 'SyncFromTraktTask' -- ansible/roles/k8s/jellyfin/CLAUDE.md`). Re-read it before
  reinstalling; it was read from the `v30` tag and a Jellyfin 12 port on `v31`+ could restructure any
  of it.

### SSO-Auth: installed #1648, removed #1674 on 2026-09-10

It was the only auth layer in front of the public route beyond Jellyfin's own local accounts. #1674
identified it as a blocker on the Jellyfin 12 image line: upstream `9p4/jellyfin-plugin-sso`
publishes no 12 build, and its newest release (`4.0.0.4`) still declares `targetAbi 10.11.0.0`. The
operator's call was to remove it and accept local accounts on the route, with `use_authelia: true` —
which protects the route but breaks native clients that cannot complete Authelia's browser login —
declined explicitly. Re-raising that is a decision, not a fix.

Authelia's `jellyfin` OIDC client went in the same change. `authelia_client_password_hash` is left in
SOPS, unreferenced by the k8s role, because the archived Docker authelia role still names it.

## Why the snapshot cap is 16 GiB and deliberately loose

Longhorn picks the number, not you: it accepts `"0"` or a value no smaller than `Volume.Spec.Size` ×
2, and it raises the value to exactly that when a volume is expanded. So 16 GiB is the smallest legal
cap for an 8Gi PVC, and `jellyfin_k8s_snapshot_max_size` is derived rather than written out — a
hardcoded one goes illegal the moment the PVC is raised past half of it, and the admission webhook
then refuses the patch on every jellyfin deploy.

16 GiB against about 1.9 GiB of live snapshots (2026-09-10) is loose on purpose rather than tuned to
usage, because **a reached cap refuses rather than prunes**: Longhorn stops accepting new snapshots
until some are deleted, and `k8s/volume-snapshot` snapshots BEFORE it prunes — so a cap tight enough
to be hit would latch, with the snapshot failing, the deploy failing, and the prune that would free
headroom never running. The weekly Longhorn backup job snapshots the same volume and would fail with
it. Filed as #1560, unreachable at today's headroom. `actualSize` was not used to size the cap: it
counts allocated blocks, including the snapshot chain, so it overshoots what the filesystem holds.
