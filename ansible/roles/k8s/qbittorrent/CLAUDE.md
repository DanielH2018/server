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
