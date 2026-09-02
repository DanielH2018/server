# qbittorrent — torrent client behind a WireGuard sidecar

qBittorrent with a `wireguard` sidecar init container that tunnels all egress through
Mullvad. See repo-root `CLAUDE.md` for shared conventions.

## Traps

### A VPN kill-switch outlives the container it fenced
On 2026-08-16 one momentary registry blip cost 9 hours down and 107 restarts.

The `wireguard` sidecar init container fetches `linuxserver/mods:wireguard-mullvad` from
lscr.io at every container start. The mod writes `/config/wg_confs/wg0.conf` and installs
iptables rules that REJECT anything not leaving via `wg0`, permitting only `LAN_NETWORKS`.
Its startup probe is `wg show wg0`, failure 60 × 5s, so a pod with no tunnel is killed and
restarted every ~5 minutes.

The deadlock: iptables rules live in the pod's network namespace, and the netns survives
container restarts. Once the tunnel dropped, the kill-switch stayed behind with nothing to
exit through. `LAN_NETWORKS` includes `10.42.0.0/15`, which covers the `10.43.x` service
CIDR, so cluster DNS kept resolving normally while every external connection was rejected.
The mod fetch failed as `[mod-init] (ERROR) No response from lscr.io` — not a DNS error —
then `OFFLINE: ... not found in modcache, skipping`, so `wg0.conf` was never written, so
`wg0` never came up, so the rules never lifted. Self-sustaining.

Recovery is `delete pod`, never a container restart. Only a new pod gets a new netns.

What made the diagnosis certain: lscr.io answered the host fine (`HTTP 401` in 0.36s, the
expected unauthenticated reply) and cluster DNS returned three A records with an empty AAAA,
so it was neither an outage nor a Pi-hole AAAA problem. The decisive evidence came after —
the replacement pod downloaded the mod from the same registry minutes later. A registry that
gives no response to one pod while serving another at the same instant is not the broken
party. Generalise: when a network failure is scoped to one pod, suspect the pod's own netns
before the remote host.

The failure is also invisible while it happens: the init container exits **0** ("Completed"),
so it reads as a restart loop rather than an error.

**The standing design risk is now addressed — see the next section.** The sidecar mounts a
persistent `/modcache`, so a start that cannot reach lscr.io applies the cached mod instead of
stranding. What has *not* changed is that a mod is still fetched over the network on a cold
cache; the cache makes the failure survivable, not impossible.

## The modcache, and the lock file it can leave behind

`deployment.yaml.j2` mounts the config claim a second time at `/modcache`, `subPath: modcache`.
That one mount is the whole fix, and the reason it works is in LinuxServer's `docker-mods.v3`:

| line | behaviour |
|---|---|
| 385, 394 | `MOD_OFFLINE="true"` is set **automatically** when the registry lookup fails — there is no env var to add |
| 403 | cached tarball present and its sha256 matches the registry's layer → apply from cache |
| 405 | cached tarball present and offline → `OFFLINE: … found in modcache`, apply it |
| 408 | tarball absent and offline → `OFFLINE: … not found in modcache, skipping` — **the line from the 2026-08-16 incident** |
| 431-438 | a successful download writes the tarball into `/modcache` itself — the cache is self-populating |

So the fallback already existed and already fired during the outage. It had nothing to fall back
to only because line 257 creates `/modcache` inside the container, where it died with each pod.

A `subPath` of the existing claim rather than a PVC of its own: the tarball is a few MB against
1Gi, and a second claim would add a storage-class decision and a backup surface for a cache any
successful start can rebuild.

### verify.yml waits for the rollout, and has to
`roles/k8s/manifests` does not wait for a rollout — it **queues** it, and `roles/k8s/rollout-drain`
runs `rollout status` for the whole batch afterwards. So a role's own `verify.yml` runs *before*
its rollout finishes and `get pod -l app=qbittorrent` returns the **outgoing** pod. That went
unnoticed here for as long as every proof held for the old pod too: a tunnel and a return path
look identical either side of a roll. Proof 3 is the first assertion whose answer differs, and it
failed the 2026-08-27 deploy against a pod that was already `Terminating` while the correctly
mounted new pod came up seconds later.

`verify.yml` now gates on `rollout status` before finding the pod. It is one inline wait for one
role, not a reversal of the batch drain, and it is a no-op when nothing rolled.

Two primitives that look like they solve this and do not, both because they are satisfied by the
**outgoing** pod:

| primitive | why it returns instantly mid-roll |
|---|---|
| `wait --for=condition=Available deploy/<x>` | Available is true of the old ReplicaSet |
| `wait --for=condition=ready pod -l app=<x>` | the old pod is still Ready until it stops |
| `--field-selector status.phase=Running` | `.status.phase` stays `Running` while Terminating — that word is kubectl's rendering of `deletionTimestamp`, not a phase |

**This exposure was not unique to this role.** The same gap was found on 2026-08-27 in
`roles/k8s/jellyfin` and `roles/k8s/tdarr` (pod lookup with no wait at all) and in
`roles/k8s/janitorr` (a `wait --for=condition=ready pod` the old pod satisfies). All three are
fixed. None had been caught because, as here, their assertions happened to hold on both sides
of a roll.

**Don't re-derive this by hand.** `ansible/tests/deploy/test_inline_rollout_gates.py` now decides it:
any role that looks up a pod by its own app label must gate on `rollout status` first, and the
check flattens included *and* imported task files so it can see a `verify.yml`. A new role with
this shape fails the suite rather than waiting for a deploy to fail.

### The new failure mode — a stale lock
Lines 413-421 hold `/modcache/<name>.lock` for the duration of a download. A pod killed
mid-download leaves it behind, and later starts **wait on it and then skip the mod** — which
presents as no tunnel and a restarting pod, *the same symptom as the outage this cache exists to
prevent*. The script says so itself: "If no other containers are using this mod you may need to
delete /modcache/<name>.lock". `verify.yml` warns when a lock is present rather than failing,
because a lock is legitimate while a download is genuinely in flight.

Diagnosing it: if the sidecar logs a skip or a lock timeout rather than `Downloading` or
`found in modcache`, delete the lock file from the volume and delete the POD (not the
container — the kill-switch lives in the netns, per the trap above).

## Throughput settings live on the PVC, not in this role

The role templates two of qBittorrent's settings and no others: `WEBUI_PORT` and
`TORRENTING_PORT`, which the LinuxServer image applies from the environment at every
start. Everything else — connection limits, hashing threads, the libtorrent working-set
bound — lives in `qBittorrent.conf` on the `qbittorrent-config` Longhorn PVC. A WebUI
change to any of it is live state that no Ansible run reproduces and a volume restore
silently reverts.

`files/apply_prefs.py` is the repo-side source of truth for the eight settings that were
tuned for throughput on 2026-08-26. Run it after a PVC restore, or after changing a value
in its `DESIRED` dict:

```bash
QBT_USERNAME=$(sops -d --extract '["qbittorrent_username"]' ansible/vars/secrets.yml) \
QBT_PASSWORD=$(sops -d --extract '["qbittorrent_password"]' ansible/vars/secrets.yml) \
QBT_URL=http://<pod-ip>:8080 \
uv run python ansible/roles/k8s/qbittorrent/files/apply_prefs.py --dry-run
```

It diffs before writing, sends only the keys that differ, and reads back to prove the
write — so a second run reports "nothing to do" rather than rewriting.

**It is deliberately NOT wired into `deploy.yml`.** A deploy renders manifests, which
fires the central rollout-restart, and the replacement pod waits on the wireguard
sidecar's startupProbe (`failureThreshold: 60 × 5s`). A prefs task in the deploy path
would block on that window every time, and an lscr.io blip during it — the failure above —
would surface as a *failed deploy* instead of as the mod-fetch problem it is. Keep the
apply manual.

### The daily drift check is log-only, and does not apply anything

Nothing used to re-run `apply_prefs.py` or notice when the PVC's live preferences drifted from
`DESIRED` (2026-08-27 review, Medium). `tasks/main.yml` now installs a daily host cron on
daniel-box (`qbittorrent_k8s_prefs_check_cron_hour`/`_minute`, `cron_file:
qbittorrent-prefs-check`) that copies `files/apply_prefs.py` to
`/opt/qbittorrent-prefs-check/` and runs it with `--dry-run` from
`templates/prefs-check.sh.j2`, then `logger -t qbittorrent-prefs-check`s the result. It never
writes to qBittorrent — applying a changed value stays the manual step documented above.

**Deliberately NOT a Kuma monitor.** The 2026-08-27 review that found this gap also found six
live Kuma push tokens leaking into `curl` argv across the estate, one world-readable — adding a
seventh for a low-urgency drift check would grow the exact class being remediated in the same
review. `logger` writes to the `{job="syslog"}` Loki stream instead, which
`scripts/diagnostics/probe.py alerts` already reads. This is why `prefs-check.sh.j2` is absent
from `ansible/tests/setup/test_cron_scripts_publish_via_pr.py`'s push-script corpus (it holds neither
`api/push` nor `PUSH_URL`) — it isn't a push script and doesn't need to sit in the shared
`kuma-push-lib.sh` contract that file enforces.

**Credentials are the existing `qbittorrent_username`/`qbittorrent_password` SOPS keys** —
the same ones `homepage`'s widget already renders — projected into
`/usr/local/bin/qbittorrent-prefs-check.sh` at 0700 owner `{{ sys_user }}` (the same shape as
`janitorr-health.sh.j2`'s Kuma token: interpolated directly rather than split into a separate
env file, since only one user ever needs to read this one). No new SOPS surface.

**apply_prefs.py's exit codes are a contract now, not an accident**: `EXIT_OK` (0),
`EXIT_UNREACHABLE` (1), `EXIT_BAD_ARGS` (2), `EXIT_DRIFT` (3, `--dry-run` only). Before
2026-08-27, `--dry-run` returned 0 whether or not anything had drifted, which is why the cron
above could not previously have branched on it. No caller shelled out to this script before
that date (grep confirmed), so this widened the contract rather than breaking one.

### The login trap
qBittorrent 5.2.3 answers a successful `POST /api/v2/auth/login` with **HTTP 204 and an
empty body**. Older builds answered `200 "Ok."`, and a client that checks for that string
rejects a login that in fact succeeded. Check for the `QBT_SID` cookie instead — a bad
password returns 200 `"Fails."` and sets no cookie, so the cookie means the same thing on
both versions. `web_ui_max_auth_fail_count` is 5, so don't debug a login by retrying it.

### Why these values, in one line
The pod egresses through Mullvad, which forwards no ports, so qBittorrent can only pair
with peers it dials itself. Every raised limit is about dialing faster and wider; none of
it substitutes for an inbound port. See `files/apply_prefs.py` for the per-setting reasons.
