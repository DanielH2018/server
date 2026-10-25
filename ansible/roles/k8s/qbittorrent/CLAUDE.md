# qbittorrent — torrent client behind a WireGuard sidecar

qBittorrent with a `wireguard` sidecar that tunnels all egress through Mullvad.

This file holds the rules; `docs/qbittorrent-vpn-and-prefs.md` holds the record — the two outages,
how the gate was measured, LinuxServer's modcache behaviour line by line, the cross-role rollout-gate
finding and why the prefs plane is shaped this way.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=qbittorrent` with exit 5
- **Images:** `lscr.io/linuxserver/qbittorrent` (`qbittorrent_k8s_image`),
  `lscr.io/linuxserver/wireguard` (`qbittorrent_k8s_wireguard_image`), `python`
  (`qbittorrent_k8s_prefs_check_image`)
- **Route:** `qbittorrent.<domain>` · `qbittorrent.local.<domain>`, Authelia one_factor
- **Claims:** `qbittorrent-config` (weekly -> B2 (default target)), `media-data` (not Longhorn
  (media-local))
<!-- /generated_from -->

- **What "state coupled outside the volume" means:** a snapshot revert of `qbittorrent-config`
  cannot undo what the tracker and the `/data/torrents` tree already saw. `media-data` is the
  shared claim — mounted here, owned elsewhere.

- **Flux applies it, not Ansible** (D5, Wave D). Its `containers_list` entry carries
  `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it. A merged change
  reaches the cluster when `promote` publishes and verifies the next bundle. To give it back
  to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of the
  flag leaves two writers on the objects. `k8s_autodeploy: false` still stops Renovate's
  automerge of a pin bump.
- **The Mullvad account reaches the pod through a SOPS manifest.** `mullvad_account` is all
  digits, which no Flux placeholder can carry as a string, so `render_sops_manifests` ships
  `mullvad-account-secret.yaml` as D3's SOPS-encrypted copy, and the `qbittorrent`
  Kustomization renders `decryption`.

## Traps

### A VPN kill-switch outlives the container it fenced
The mod's iptables rules live in the **pod's** network namespace, which survives a container restart,
and the sidecar's own mod fetch and Mullvad API call route through the dead `wg0` they fence — so a
dropped tunnel used to sustain itself until someone deleted the pod (9 hours on 2026-08-16, 3.5 days
on 2026-09-13, #1838). It presents as a restart loop rather than an error, because the init container
exits **0**. The netns reset below clears it without a human, and `delete pod` is the escape hatch if
that reset is ever removed.

## The sidecar resets the netns on every start

`files/netns-reset.sh` is the wireguard container's entrypoint wrapper, mounted from the
`qbittorrent-netns-reset` ConfigMap and running before the image's s6 init. It clears the wg0 link,
wg-quick's policy rules and LAN routes, the OUTPUT chain and the raw/mangle tables.

- **The gate goes up before the tunnel comes down, and that order is the privacy argument.** Its
  first act is one atomic `iptables-restore` installing the uid-owner REJECT, and **a failed restore
  exits 1 and touches nothing** — the old deadlock, never a leak.
- **The gate exempts fwmark 51820, and must.** WireGuard's carrier packets still belong to
  qbittorrent's socket, so a gate without that exemption leaves a Ready pod at `DHT: 0 nodes`.
  `ansible/tests/services/test_qbittorrent_sidecar_resets_the_netns.py` holds it.
- **The reset does not fetch the mod or bring the tunnel up.** A start whose API call still fails
  restarts every ~5 minutes on the startup probe, gate holding.

## The modcache, and the lock file it can leave behind

`deployment.yaml.j2` mounts the config claim again at `/modcache` (`subPath: modcache`), and that one
mount is the whole fix: `docker-mods.v3` already falls back to a cached tarball when the registry
lookup fails, and before the mount the cache died with each pod. **A cold cache still fetches over
the network** — survivable, not impossible.

Its one new failure mode: a pod killed mid-download leaves `/modcache/<name>.lock` behind, and later
starts **wait on it and then skip the mod** — *the same symptom as the outage this cache prevents*.
The verify Job warns rather than fails on a lock, which is legitimate mid-download. If the sidecar
logs a skip or a lock timeout rather than `Downloading` or `found in modcache`, delete the lock file.

## Throughput settings live on the PVC, not in this role

The role templates `WEBUI_PORT` and `TORRENTING_PORT` and nothing else. Connection limits, hashing
threads and the libtorrent working-set bound live in `qBittorrent.conf` on the `qbittorrent-config`
PVC, so **a WebUI change to any of them is live state no Ansible run reproduces and a volume restore
silently reverts.**

`files/apply_prefs.py` is the repo-side source of truth for the settings tuned for throughput on
2026-08-26. It diffs before writing, sends only the keys that differ, and reads back to prove the
write, so a second run reports "nothing to do".

**`DESIRED` also holds the excluded-file-name list, which is not a throughput setting.**
qBittorrent marks a file matching `*.exe`, `*.7z`, `*.iso` or another listed pattern "do not
download" when a torrent is added (#6274). A fake episode that is only such a file then
downloads and seeds nothing. Sonarr still flags the empty download, so Arr Queue Warnings still
pages and a human still removes it. `.rar` stays off the list, because scene TV ships as
multi-part RAR. The comment above the keys in `apply_prefs.py` has the observed fake shapes.

**Flux applies `DESIRED` on every revision that rolls the pod.** The `prefs.yaml.j2` Job in
`qbittorrent-verify` (below) runs the apply once the `qbittorrent` Kustomization reads Ready, so a
changed value in `DESIRED` goes live on the next roll without a human. While Ansible owned the role
the apply was manual, because a deploy task would have waited out the sidecar's five-minute
startupProbe window. Run `apply_prefs.py` by hand after a PVC restore that no roll follows — the
docs page has the command.

### The daily drift check is log-only, and does not apply anything

The `qbittorrent-prefs-check` CronJob (`templates/prefs-check-cronjob.yaml.j2`, schedule
`ansible/roles/k8s/qbittorrent/defaults/main.yml:qbittorrent_k8s_prefs_check_schedule` on
Etc/UTC) runs `files/prefs_check.py`, which runs `files/apply_prefs.py --dry-run` against the
WebUI Service and prints one `qbittorrent-prefs-check: <verdict>` line to the pod log. **It never
writes to qBittorrent** — the prefs Job applies on a roll, and a hand run applies between rolls. It is deliberately not a Kuma
monitor and reads the script's exit codes as a contract (`EXIT_DRIFT` is 3, `--dry-run` only);
the docs page has why.

- **The pod carries `app: qbittorrent-prefs-check`, never `app: qbittorrent`.** The WebUI
  Service and the compat `wireguard` Service select `app=qbittorrent`.
  qbittorrent's `netpol_from` admits the check's label, and this role renders that fence.
- **The login reaches it from the `qbittorrent-prefs-check` Secret** as one two-line value,
  `QBT_CREDENTIALS` (`templates/prefs-check-secret.yaml.j2` says why it is not a key each). The
  ConfigMap that stages the code holds no secret.
- **Flux gates it in `qbittorrent-verify`.** `flux_cronjob_gates` in `defaults/main.yml` names
  it, so the renderer copies its jobTemplate into `qbittorrent-prefs-check-deploy-gate-<spec
  digest8>`, a verify gate. The Job starts once the tag is Ready, so it meets a rolled-out pod
  rather than the sidecar's startup window. It carries no ownerReference to the CronJob, so
  `concurrencyPolicy: Forbid` no longer keeps the 06:37 firing off it; the check only reads, so
  an overlap is harmless.
- **It was a daniel-box host cron until H2.5d.** The absent arms for the cron,
  `/usr/local/bin/qbittorrent-prefs-check.sh` and `/opt/qbittorrent-prefs-check` sit in
  `ansible/roles/setup/k8s_host_files/tasks/qbittorrent.yml`, which the setup plane applies
  through `initial_setup.yml`. D5's conversion PR moved them there from `tasks/main.yml`.

### The login trap
qBittorrent 5.2.3 answers a successful `POST /api/v2/auth/login` with **HTTP 204 and an empty
body**, where older builds answered `200 "Ok."` — so a client checking for that string rejects a
login that succeeded. Check for the `QBT_SID` cookie instead; a bad password sets none.
`web_ui_max_auth_fail_count` is 5, so don't debug a login by retrying it.

## The Flux Jobs in `templates/verify/`

The renderer emits two Jobs from that directory into `qbittorrent-verify`, beside the drift
check's gate Job. Both run `files/qbittorrent_job.py` with no `pods/exec` grant:

- **`job.yaml.j2` replaced the Ansible verify tasks at the flip.** The Job dials the WebUI from the pod CIDR,
  lists the mod cache through its own read-only mount of the sidecar's claim and subPath, and
  compares qBittorrent's `last_external_address_v4` with its own egress address. The script's
  docstring maps each proof to its Job form.
- **`prefs.yaml.j2` runs the prefs apply on every revision that rolls the pod.** The sidecar's
  startup window has passed by then, because `<tag>-verify` starts once the tag is Ready. It
  reruns safely: a second run writes nothing.
- **Their pods reuse two labels the fence already admits**, `qbittorrent-reach-probe` and
  `qbittorrent-prefs-check`; each template's `DECIDED:` note says why.
- **The script cannot import `apply_prefs.py`**, because the Job census admits no
  `urllib.request`. Its `DESIRED` is a copy, and `tests/test_qbittorrent_job.py` holds the two
  equal, renders the directory through the Job census, and runs each check against a
  stand-in WebUI.
- **The first live run proves two assumptions, and each fails closed.** A PUID-1000 pod lists
  the sidecar's mod cache through a second read-only mount of the ReadWriteOnce claim, and
  qBittorrent reports a non-empty `last_external_address_v4` within 120 seconds. If either is
  wrong, `qbittorrent-verify` fails and holds the revision.
