# qbittorrent record — the kill-switch deadlock, the netns reset and the prefs plane

Working-out moved off `ansible/roles/k8s/qbittorrent/CLAUDE.md` (#2991), which a session reads on
every touch of the role. The role doc keeps the rules; this page keeps the two outages behind them,
the measurements the netns-reset gate was proven with, LinuxServer's `modcache` behaviour line by
line, and why the prefs plane is shaped the way it is.

## The 2026-08-16 kill-switch deadlock

One momentary registry blip cost 9 hours down and 107 restarts.

The `wireguard` sidecar init container fetches `linuxserver/mods:wireguard-mullvad` from lscr.io at
every container start. The mod writes `/config/wg_confs/wg0.conf` and installs iptables rules that
REJECT anything not leaving via `wg0`, permitting only `LAN_NETWORKS`. Its startup probe is `wg
show wg0`, failure 60 × 5s, so a pod with no tunnel is killed and restarted every ~5 minutes.

The deadlock: iptables rules live in the pod's network namespace, and the netns survives container
restarts. Once the tunnel dropped, the kill-switch stayed behind with nothing to exit through.
`LAN_NETWORKS` includes `10.42.0.0/15`, which covers the `10.43.x` service CIDR, so cluster DNS
kept resolving normally while every external connection was rejected. The mod fetch failed as
`[mod-init] (ERROR) No response from lscr.io` — not a DNS error — then `OFFLINE: ... not found in
modcache, skipping`, so `wg0.conf` was never written, so `wg0` never came up, so the rules never
lifted. Self-sustaining.

What made the diagnosis certain: lscr.io answered the host fine (`HTTP 401` in 0.36s, the expected
unauthenticated reply) and cluster DNS returned three A records with an empty AAAA, so it was
neither an outage nor a Pi-hole AAAA problem. The decisive evidence came after — the replacement
pod downloaded the mod from the same registry minutes later. A registry that gives no response to
one pod while serving another at the same instant is not the broken party. **Generalise: when a
network failure is scoped to one pod, suspect the pod's own netns before the remote host.**

The failure is also invisible while it happens: the init container exits **0** ("Completed"), so it
reads as a restart loop rather than an error.

## The 2026-09-13 recurrence, and why the state is self-sustaining twice

It recurred with a different trigger and the same deadlock (#1838). The mod's first API call failed
with `curl: (6) Could not resolve host: api.mullvad.net (Could not contact DNS servers)` after the
previous container's tunnel had dropped, and 1005 sidecar restarts over 3.5 days never cleared it.

The stale state sustains itself twice over, not once: the API call routes into the dead wg0 and
fails, and even a successful call ends in wg-quick's PostUp hitting the LAN routes the previous
PostUp already added (`RTNETLINK answers: File exists`, the fatal case documented on
`qbittorrent_k8s_lan_networks`), so the tunnel is torn back down.

## How the netns reset was measured, and the leak the first deploy found

Measured in the real image on daniel-pi (2026-09-17, `linuxserver/wireguard` under Docker with
NET_ADMIN+NET_RAW, staged stale state, then the script): the after-state had no wg0, no
fwmark/suppress rules, no LAN routes, empty raw/mangle, and the OUTPUT chain reduced to the gate; a
second run on the clean netns was a no-op. Through the gate, `curl https://1.1.1.1` as uid 1000 was
refused (`curl: (7)`), as root it reached the host, and a LAN address passed. To rerun it, copy the
script to the Pi and stage the state inside the image the same way — the cluster nodes refuse
unprivileged user namespaces, so there is no sandbox on them.

**That measurement never sent uid-1000 traffic through a live tunnel, and the first deploy did.** A
packet qbittorrent sends into wg0 leaves the pod as an encrypted UDP carrier on eth0, and the
carrier still belongs to the originating socket: `--uid-owner 1000` matched it, `! -o wg0` was true,
and the gate rejected it. Deployed 2026-09-17 15:20, the tunnel stayed up (the liveness curl runs
as root), the pod read 2/2 Ready, and qbittorrent saw only timeouts — `DHT: 0 nodes`, every tracker
and peer dead, `s6-setuidgid abc curl https://1.1.1.1` timing out where root's succeeded. The gate
now exempts `fwmark` 51820 (`wg set wg0 fwmark`) exactly as the mod's own REJECT does, and
`test_the_gate_exempts_wireguards_own_carrier_packets` holds it.

**Verify a gate change by the REJECT rule's packet counter after a uid-1000 attempt** —
`iptables -Z OUTPUT; s6-setuidgid abc curl …; iptables -L OUTPUT -v -n` — never by root's curl, since
root is the uid the gate exempts.

The script installs the gate with one atomic `iptables-restore`: the `LAN_NETWORKS` ACCEPTs and
`! -o wg0 -m owner --uid-owner $PUID ... -j REJECT`, so every off-LAN packet from qbittorrent's uid
that is not leaving via wg0 is refused while root's (the mod's API calls) is not. The mod's own
mark-exempt REJECT lands after it; both stay, and they agree wherever they overlap.

## What LinuxServer's `docker-mods.v3` does with `/modcache`

| line | behaviour |
|---|---|
| 385, 394 | `MOD_OFFLINE="true"` is set **automatically** when the registry lookup fails — there is no env var to add |
| 403 | cached tarball present and its sha256 matches the registry's layer → apply from cache |
| 405 | cached tarball present and offline → `OFFLINE: … found in modcache`, apply it |
| 408 | tarball absent and offline → `OFFLINE: … not found in modcache, skipping` — **the line from the 2026-08-16 incident** |
| 413-421 | `/modcache/<name>.lock` is held for the duration of a download |
| 431-438 | a successful download writes the tarball into `/modcache` itself — the cache is self-populating |

So the fallback already existed and already fired during the outage. It had nothing to fall back to
only because line 257 creates `/modcache` inside the container, where it died with each pod.

The mount is a `subPath` of the existing config claim rather than a PVC of its own: the tarball is
a few MB against 1Gi, and a second claim would add a storage-class decision and a backup surface
for a cache any successful start can rebuild.

A pod killed mid-download leaves the lock behind, and later starts wait on it and then skip the
mod. The script says so itself: "If no other containers are using this mod you may need to delete
/modcache/<name>.lock".

## The rollout-gate exposure was not unique to this role

`roles/k8s/manifests` queues a rollout and runs `rollout status` for the whole batch afterwards, so a role's own `verify.yml` runs before its rollout finishes and `get pod -l app=<x>` returns the outgoing pod. qbittorrent's verify tasks gated on `rollout status` for that reason until its Flux flip replaced them with the `qbittorrent-verify` Job, which starts only once the `qbittorrent` Kustomization reads Ready. `wait --for=condition=Available`, `wait --for=condition=ready pod` and `--field-selector status.phase=Running` all return instantly mid-roll, because the old pod satisfies each of them.

## The whole `apply_prefs.py` command

Flux applies `DESIRED` on every revision that rolls the pod, through the `prefs.yaml.j2` Job
in `qbittorrent-verify`. A hand run is for a PVC restore that no roll follows, or to read
drift between rolls.

Run this from the repo root, against the live pod's IP, with `--dry-run` first. It is
**operator-only**: the agent guard denies `sops -d`, `--extract` included, because the
credentials would land in the transcript. An agent hands the operator the command instead.

```bash
QBT_USERNAME=$(sops -d --extract '["qbittorrent_username"]' ansible/vars/secrets.yml) \
QBT_PASSWORD=$(sops -d --extract '["qbittorrent_password"]' ansible/vars/secrets.yml) \
QBT_URL=http://<pod-ip>:8080 \
uv run python ansible/roles/k8s/qbittorrent/files/apply_prefs.py --dry-run
```

## Why the prefs plane is shaped the way it is

Nothing used to re-run `apply_prefs.py` or notice when the PVC's live preferences drifted from
`DESIRED` (2026-08-27 review, Medium). A daily check closed that. It ran as a daniel-box host
cron until H2.5d, and runs as the `qbittorrent-prefs-check` CronJob since
(`ansible/roles/k8s/qbittorrent/templates/prefs-check-cronjob.yaml.j2`, 06:37 UTC). Its entry
point, `files/prefs_check.py`, runs `apply_prefs.py --dry-run --connect-wait 60` and prints one
`qbittorrent-prefs-check: <verdict>` line to the pod log. Read the verdicts with
`uv run python scripts/diagnostics/probe.py loki-query '{container="qbittorrent-prefs-check"}' --since 48h`.
Five decisions inside it are deliberate:

- **Not a Kuma monitor.** The 2026-08-27 review that found this gap also found six live Kuma push
  tokens leaking into `curl` argv across the estate, one world-readable. Adding a seventh for a
  low-urgency drift check would grow the exact class being remediated in the same review, so the
  check pushes nothing and the pod log is the record.
- **No new SOPS surface.** It reuses the `qbittorrent_username`/`qbittorrent_password` keys that
  `homepage`'s widget already renders. They reach the pod from the `qbittorrent-prefs-check`
  Secret as one value, `QBT_CREDENTIALS`, the username and the password on two lines. One value
  rather than a key each, because a key whose whole value is one secret is a standalone Flux
  placeholder. That would add both keys to
  `ansible/roles/k8s/manifests/files/standalone_secret_keys.json`, and a change to that file fans
  the landing's narrowed apply out to every `k8s/manifests` caller. The ConfigMap that stages the
  code holds no secret (`ansible/tests/k8s/test_no_secret_in_a_moved_jobs_configmap.py`).
- **The exit codes are a contract**: `EXIT_OK` (0), `EXIT_UNREACHABLE` (1), `EXIT_BAD_ARGS` (2),
  `EXIT_DRIFT` (3, `--dry-run` only). Before 2026-08-27, `--dry-run` returned 0 whether or not
  anything had drifted, so a scheduled caller could not have branched on it. `prefs_check.py`
  reads drift as a result and exits 0 for it, as for a match. Any other exit leaves the Job
  Failed. On the gate run that fails `qbittorrent-verify` and holds the revision; on a
  scheduled run it is a Failed Job and nothing more.
- **The connect retry polls, and the login runs once.** A new pod's first dials can be dropped
  even through an open fence, because kube-router learns a new pod's labels late.
  `--connect-wait 60` retries an unauthenticated `GET /` that gets no response at all for up to
  60 seconds. Any HTTP answer, an error status included, ends the poll. The login that follows is
  never retried, because `web_ui_max_auth_fail_count` is 5.
- **Each revision runs it once, after the rollout.** `flux_cronjob_gates` in the role's defaults
  makes the renderer copy the CronJob into a gate Job in `qbittorrent-verify`, which starts
  once the `qbittorrent` Kustomization reads Ready. A gate run straight after the apply would
  meet the replacement pod inside the sidecar's startup window and log `cannot reach`.
  qbittorrent's `netpol_from` admits the `qbittorrent-prefs-check` label, and the role renders
  that fence itself, so the fence and the CronJob arrive in one apply. Until the Flux flip,
  `tasks/main.yml` ran the same gate through `k8s/cronjob-gate` after the verify tasks.

**Why the values themselves.** The pod egresses through Mullvad, which forwards no ports, so
the client can only pair with peers it dials itself. Every raised limit is about dialing faster
and wider; none of it substitutes for an inbound port, and
`ansible/roles/k8s/qbittorrent/files/apply_prefs.py` has the per-setting reasons.
