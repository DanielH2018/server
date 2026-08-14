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
The pod's upsmon (primary) only **raises FSD** — it cannot power off the node. The HOST
runs `nut-client` with a `secondary`-mode upsmon (see the `nut_host` role, wired in
`initial_setup.yml`) watching `apc-ups@127.0.0.1` over the hostPort; it performs the real
`systemctl poweroff`. Sequence: 120 s on battery (upssched) or LOWBATT → pod
`upsmon -c fsd` → host secondary powers off.

**Manual shutdown drill** (actually powers daniel-server off — have console access):
`kubectl -n homelab exec deploy/nut -- upsmon -c fsd` → host poweroff within ~15 s (HOSTSYNC).

## Editing
- Manifests/config: `templates/*.j2` (config-secret.yaml.j2 holds all six NUT files)
- Deploy (on daniel-box): `uv run ansible-playbook ansible/deploy.yml --tags "nut"`
- Host half (udev rule, secondary upsmon): `ansible/roles/nut_host/`, via
  `initial_setup.yml --tags nut_host` on daniel-server
