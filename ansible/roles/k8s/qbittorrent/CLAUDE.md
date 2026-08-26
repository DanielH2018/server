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

The standing design risk is not fixed. Fetching a mod over the network on every start means
any lscr.io blip can strand the service indefinitely, and the failure is invisible — the init
container exits **0** ("Completed"), so it reads as a restart loop rather than an error. A
populated modcache would make the mod survive an outage.

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
