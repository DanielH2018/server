# qbittorrent — torrent client behind a WireGuard sidecar

qBittorrent with a `wireguard` sidecar that tunnels all egress through Mullvad.

This file holds the rules; `docs/qbittorrent-vpn-and-prefs.md` holds the record — the two outages,
how the gate was measured, LinuxServer's modcache behaviour line by line, the cross-role rollout-gate
finding and why the prefs plane is shaped this way.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "qbittorrent"`
- **Images:** `lscr.io/linuxserver/qbittorrent` (`qbittorrent_k8s_image`),
  `lscr.io/linuxserver/wireguard` (`qbittorrent_k8s_wireguard_image`), `alpine`
  (`qbittorrent_k8s_probe_image`)
- **Route:** `qbittorrent.<domain>` · `qbittorrent.local.<domain>`, Authelia one_factor
- **Claims:** `qbittorrent-config` (weekly -> B2 (default target)), `media-data` (not Longhorn
  (media-local))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — state coupled outside the volume —
  reverting qbittorrent-config to a snapshot rewinds in-flight torrent bookkeeping while the
  media-data volume it references does not move; the pre-apply snapshot and revert work fine
  and are not the blocker
<!-- /generated_from -->

- **What "state coupled outside the volume" means:** a snapshot revert of `qbittorrent-config`
  cannot undo what the tracker and the `/data/torrents` tree already saw. `media-data` is the
  shared claim — mounted here, owned elsewhere.

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
`verify.yml` warns rather than fails on a lock, which is legitimate mid-download. If the sidecar logs
a skip or a lock timeout rather than `Downloading` or `found in modcache`, delete the lock file.

### verify.yml waits for the rollout, and has to
`roles/k8s/manifests` **queues** the rollout rather than waiting, so a role's own `verify.yml`
otherwise runs against the **outgoing** pod — which passed every proof here until one mount assertion
differed and failed the 2026-08-27 deploy. `verify.yml` now gates on `rollout status` before finding
the pod, a no-op when nothing rolled.

ENFORCED by `ansible/tests/deploy/test_inline_rollout_gates.py`: any role looking up a pod by its own
app label gates on `rollout status` first. The docs page names the three other roles that carried the
gap and the primitives that look like a fix and are not.

## Throughput settings live on the PVC, not in this role

The role templates `WEBUI_PORT` and `TORRENTING_PORT` and nothing else. Connection limits, hashing
threads and the libtorrent working-set bound live in `qBittorrent.conf` on the `qbittorrent-config`
PVC, so **a WebUI change to any of them is live state no Ansible run reproduces and a volume restore
silently reverts.**

`files/apply_prefs.py` is the repo-side source of truth for the eight settings tuned for throughput
on 2026-08-26. Run it after a PVC restore, or after changing a value in its `DESIRED` dict — the docs
page has the command. It diffs before writing, sends only the keys that differ, and reads back to
prove the write, so a second run reports "nothing to do".

**It is deliberately NOT wired into `deploy.yml`.** The replacement pod waits on the sidecar's
startupProbe (`failureThreshold: 60 × 5s`), so a prefs task in the deploy path would block on that
window every time and turn an lscr.io blip into a failed deploy. Keep the apply manual.

### The daily drift check is log-only, and does not apply anything

`tasks/main.yml` installs a daily daniel-box cron
(`qbittorrent_k8s_prefs_check_cron_hour`/`_minute`, `cron_file: qbittorrent-prefs-check`) that runs
`files/apply_prefs.py --dry-run` from `templates/prefs-check.sh.j2` and `logger`s the result under
the `qbittorrent-prefs-check` tag. **It never writes to qBittorrent** — applying a changed value
stays manual. It is deliberately not a Kuma monitor and reads the script's exit codes as a contract
(`EXIT_DRIFT` is 3, `--dry-run` only); the docs page has why.

### The login trap
qBittorrent 5.2.3 answers a successful `POST /api/v2/auth/login` with **HTTP 204 and an empty
body**, where older builds answered `200 "Ok."` — so a client checking for that string rejects a
login that succeeded. Check for the `QBT_SID` cookie instead; a bad password sets none.
`web_ui_max_auth_fail_count` is 5, so don't debug a login by retrying it.
