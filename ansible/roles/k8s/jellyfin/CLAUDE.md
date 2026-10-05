# jellyfin — the media server, and the fleet's only GPU workload

Jellyfin, transcoding on the Intel GPU the `dri-device-plugin` role advertises. It reads the
shared `media-data` library and owns its own config volume.

This file holds the rules. `docs/jellyfin-plugins.md` holds the per-plugin record — what each of the
five installed plugins is, what blocks the Jellyfin 12 image line, the census of what the pod loads,
the Trakt and SSO-Auth decisions, and why the snapshot cap is the number it is.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "jellyfin"`
- **Images:** `lscr.io/linuxserver/jellyfin` (`jellyfin_k8s_image`), `python`
  (`jellyfin_k8s_plugin_init_image`)
- **Route:** `jellyfin.<domain>` · `jellyfin.local.<domain>`, no Authelia
- **Claims:** `jellyfin-config` (weekly -> B2 (default target)), `media-data` (not Longhorn
  (media-local))
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

- **The image is pinned in lockstep with the `targetAbi` of every installed plugin** — ani-sync,
  Intro Skipper, Webhook, Merge Versions and Media Cleaner. **Raise a plugin and the image together,
  never one alone**, and **each addition TIGHTENS the pin**: the constraint is `max` over the declared
  `targetAbi` values. Do not read the floor off this sentence —
  `ansible/tests/services/test_every_jellyfin_plugin_target_abi_fits_the_image.py` derives it from
  `defaults/main.yml` and covers a plugin added without its own guard. **Ask what blocks the next
  Jellyfin line before planning a bump**; the docs page has that survey.
- **`use_authelia: false` — no forward-auth middleware on a public route**, so **nothing but
  Jellyfin's own local accounts authenticates it** since SSO-Auth was removed (#1674).
  `use_authelia: true` protects the route but breaks native clients, so re-raising it is a
  decision, not a fix.
- **Persists:** `jellyfin-config` (`longhorn`, backed up, 8Gi) — the library database, artwork
  and trickplay data. `media-data` (from `k8s/media-volume`) is mounted read-only, twice.
- **GPU:** requests the `devic.es/dri` extended resource. `verify.yml` proves it is reachable
  from inside the pod as part of every deploy.
- **LAN address:** a MetalLB LoadBalancer Service on a fixed IP, typed into TV and phone clients
  by hand and asserted after apply — a MetalLB misconfiguration otherwise moves the Service to an
  auto-assigned address with the pod still healthy.

## Notable
- `k8s_autodeploy: true` despite `Recreate` + RWO storage — the pre-apply Longhorn snapshot
  `k8s/volume-snapshot` takes on `jellyfin-config` (restorable by `k8s/volume-revert`) is what makes
  that safe. `media-data` is mounted but **not** covered by that revert.
- SSDP/DLNA discovery is deliberately unsupported: MetalLB's L2 mode does not carry multicast,
  so this pod stays off `hostNetwork`.
- **The newest plugin release is routinely the wrong one.** Intro Skipper tags `10.11/v…` and
  `12.0/v…` in parallel, Webhook's newest major declares `targetAbi 12.0.0.0`, Merge Versions tags
  the release line by Jellyfin version, and Media Cleaner encodes the server ABI in its version's
  fourth segment — where **picking the wrong asset installs cleanly and never loads**.
- **An installer's shape follows its zip.** A zip carrying its own `meta.json` (Webhook, Media
  Cleaner) is unpacked as-is; a DLL-only zip (Intro Skipper, Merge Versions) needs the init container
  to write `meta.json`, or Jellyfin invents one from the directory name.
- **Plugin configuration is dashboard state on the PVC and stays there** — Media Cleaner's deletion
  rules (it **deletes media files** by rule) and Webhook's destination live under
  `/config/data/plugins/configurations/`. Pinning changes what version runs, not what it is pointed
  at.
- **`sweep-unlisted-plugins` removes every plugin this role does not install** (#2873), because
  **dropping an installer is not a removal** — the install wrote `<Name>_<Version>` onto the PVC and
  Jellyfin scans that directory every start. It runs before the installers, skips `configurations/`
  by name, and `ansible/tests/services/test_jellyfin_plugin_allowlist.py` holds its `KEEP` tuple
  equal to the set the installers write. **It matches names, not versions** by design, so a newer
  directory `Update Plugins` downloaded survives it — the next bullet removes that one.
- **Each installer holds its own version on every start** (#2905), through two steps that both sit
  OUTSIDE the `already installed` branch — the branch a restart takes. The superseded sweep removes
  every `<Name>_*` directory the pin does not name, and `autoUpdate: false` goes into the plugin's
  `meta.json`, which is what stops the fetch recurring (it defaults to TRUE). The sweep runs AFTER
  the download, so a failed install leaves the working version in place.
  `ansible/tests/services/test_jellyfin_plugin_pins_hold_across_a_restart.py` runs all five
  installers against a seeded directory.
- **Read the live plugin census from the pod's log**, not from this file:
  `kubectl -n homelab logs <pod> -c jellyfin | grep 'Loaded plugin:'`. The image bundles five of its
  own, which move with `jellyfin_k8s_image` and need no pin here.

## The snapshot-space cap on `jellyfin-config`
`tasks/main.yml` patches `spec.snapshotMaxSize` on the volume backing `jellyfin-config`, to
`jellyfin_k8s_snapshot_max_size` — 2 × `jellyfin_k8s_size`, 16 GiB today. Longhorn's default is
`"0"`, uncapped, and this role snapshots before every deploy — so without the patch the backend grows
behind a PVC that is itself capped.

- **The patch runs after `k8s/manifests`, so a brand-new volume's first snapshot is uncapped.**
  The claim is `k8s_claims`, which `k8s/manifests` applies just before it snapshots, and the
  operator chose that one uncapped snapshot over a hook in the shared role (#3387). The
  `DECIDED:` marker on the cap task carries the reasoning.

- **Longhorn picks the number, not you.** It accepts `"0"` or a value no smaller than
  `Volume.Spec.Size` × 2, so the var stays derived: a hardcoded one goes illegal the moment the PVC
  is raised past half of it, and the admission webhook then refuses the patch on every deploy.
- **A reached cap refuses, it does not prune**, and `k8s/volume-snapshot` snapshots before it
  prunes — so a cap tight enough to be hit would latch the deploy and the weekly Longhorn backup
  together (#1560). That is why the cap is deliberately loose rather than tuned to usage; the docs
  page has the headroom measurement.
- **The pre-apply snapshot stays armed.** `k8s_autodeploy: true` is justified *by* that snapshot in
  `k8s_autodeploy_reason`, so disarming it means turning auto-deploy off for the fleet's only GPU
  workload — a much larger trade than bounding backend growth.
