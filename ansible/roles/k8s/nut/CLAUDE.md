# nut — NUT primary (upsd + driver + upsmon), in-cluster

Successor to the Docker `nut` sidecar (D6 reversed 2026-08-14 so Docker could leave
daniel-server entirely). Pinned to daniel-server — the APC UPS is USB-attached there;
the pin is physical and never unwinds.

## At a glance
- **Image:** built in-cluster from `templates/Dockerfile.j2` (debian:bookworm-slim + nut).
  Config is **not** baked in — the registry is unauthenticated, and `upsd.users` carries
  passwords. It mounts from the `nut-config` Secret; the entrypoint stages it to `/etc/nut`.
- **Pull path:** daniel-server's containerd reaches the registry through the agent-side
  `registries.yaml` mirror (ClusterIP endpoint) — the seam this port re-plumbed.
- **Exposure:** hostPort `127.0.0.1:3493` (host secondary upsmon) + ClusterIP Service
  `nut:3493` (peanut web UI, Home Assistant's NUT integration).
- **privileged: true** — k8s has no compose `devices:` equivalent; a hostPath'd
  `/dev/bus/usb` is refused by a non-privileged device cgroup. See deployment.yaml.j2
  for the containment reasoning; a USB device plugin is the noted unprivileged follow-up.

## Two-tier shutdown (unchanged from the Docker era)
The pod's upsmon (primary) only **raises FSD** — it cannot power off the node. Each HOST
runs `nut-client` with a `secondary`-mode upsmon (see the `nut_host` role, wired in
`initial_setup.yml`) watching the same upsd; it performs the real `systemctl poweroff`.
Sequence: 300 s on battery (upssched) or LOWBATT → pod `upsmon -c fsd` → every armed host
secondary powers off.

**Both nodes are armed** as of 2026-08-28. daniel-server reaches upsd over the pod's
`127.0.0.1:3493` hostPort; daniel-box crosses nodes to the ClusterIP and is armed in
`host_vars/daniel-box.yml`. Before that, daniel-box ran to battery end and lost power
mid-write with etcd, the Longhorn primary replicas, Traefik and LAN DNS on it.

### Why the two poweroffs are not staggered
The pod's primary upsmon has a `logger` no-op `SHUTDOWNCMD`, so its `HOSTSYNC` gates nothing —
both real poweroffs come from peer secondaries that never coordinate with each other. The
apparent hazard is that daniel-server hosts the upsd pod, so its poweroff removes the very
service daniel-box is watching. It does not bite: a secondary needs upsd only to **observe**
FSD, not to complete `SHUTDOWNCMD`, and both hosts observe it within one `POLLFREQALERT`
(5 s) of the pod's `upsmon -c fsd` — far ahead of daniel-server's poweroff reaching
containerd. Staggering by `FINALDELAY` would additionally rest on that setting applying in
secondary mode, which `upsmon.conf(5)` documents for **primary** mode only. Hence the
`# DECIDED:` marker at `FINALDELAY` in `roles/nut_host/templates/host-upsmon.conf.j2`.

### Timer vs. safety net
`nut_onbatt_shutdown_delay` (300 s, `defaults/main.yml`) decides how long a blip is ridden out
before a planned stop. The end of the battery is a *different* trigger: `AT LOWBATT * EXECUTE
lowbatt` raises FSD as soon as the UPS asserts LB (`battery.charge.low` 10,
`battery.runtime.low` 120). Measured runtime 2026-08-28 was 987 s at 43 % load, so 300 leaves
roughly 11 minutes for the poweroffs. `ansible/tests/test_nut_host_secondary.py` asserts the
delay stays inside that band while a host beyond `ups_host` is armed.

**Manual shutdown drill** (actually powers BOTH nodes off now that daniel-box is armed — have
console access to each): `kubectl -n homelab exec deploy/nut -- upsmon -c fsd` → host poweroff
within ~15 s (HOSTSYNC).

**Known gap.** `nut_host_upsd_clusterip` is a deploy-time snapshot. The role asserts upsd is
reachable when it runs, but nothing re-checks it: if the `nut` Service is recreated with a new
ClusterIP, daniel-box silently reverts to a hard cut while every check stays green. A Kuma push
or a `monitor-bridge` check is the follow-up.

## Editing
- Manifests/config: `templates/*.j2` (config-secret.yaml.j2 holds all six NUT files)
- Deploy (on daniel-box): `uv run ansible-playbook ansible/deploy.yml --tags "nut"`
- Host half (udev rule, secondary upsmon): `ansible/roles/nut_host/`, via
  `initial_setup.yml --tags nut_host` on **both** daniel-server and daniel-box. The role's
  own `when:` (`initial_setup.yml`) is `inventory_hostname == ups_host or
  nut_host_secondary_armed`, so daniel-box is in scope from the moment its secondary was
  armed — running this on daniel-server alone re-arms the primary and silently skips the
  secondary. This line said "on daniel-server" until 2026-08-29, three lines below the
  section that already said both nodes are armed.
