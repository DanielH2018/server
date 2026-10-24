# nut — NUT primary (upsd + driver + upsmon), in-cluster

Successor to the Docker `nut` sidecar (D6 reversed 2026-08-14 so Docker could leave
daniel-server entirely). Pinned to daniel-server — the APC UPS is USB-attached there;
the pin is physical and never unwinds.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses `--tags=nut`
  with exit 5
- **Image:** `ghcr.io/danielh2018/nut` (`nut_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux's render pulls `ghcr.io/danielh2018/nut:<content tag>`**, with `imagePullPolicy:
  IfNotPresent` (D5.4b), and the *Image* line above names that repository. The role's defaults
  still name the in-cluster registry, for the handback. The image is built from
  `templates/Dockerfile.j2` (debian:bookworm-slim + nut). Config is **not** baked in, because
  the image is public on GHCR and `upsd.users` carries passwords. It mounts from the
  `nut-config` Secret, and the entrypoint stages it to `/etc/nut`.
- **Pull path:** daniel-server's containerd asks the in-cluster registry's ghcr.io pull-through
  mirror on port 5002 first, through the agent-side `registries.yaml` entry, and then ghcr.io
  itself. Under `IfNotPresent`, a restart on daniel-server uses the image the last rollout
  pulled. GHCR or the mirror is needed only for a new content tag, or after kubelet's image
  garbage collection removed the image. A mirror that has already served the tag lets a cold
  pull succeed during a WAN outage, the case D5 Open question 2 kept the mirror for. The image is
  41.65 MB in four layers. On 2026-10-18 its largest layer (28.2 MB) pulled at 0.36-0.45 MB/s
  from GHCR or through a cold mirror, and in 0.4 s from a warm one. A cold pull of the whole
  image takes about two minutes, well inside Flux's default 600s `timeout`.
- **The role also renders `nut-ghcr-mirror-probe`**, a ConfigMap holding the repository and
  content tag node-exporter's `ghcr-mirror-pull` sidecar GETs through each node's mirror
  (#5542). Only this role's render sees its own tag. No nut pod names the ConfigMap, so it
  adds nothing to the restart checksum the renderer stamps on nut, and adding it rolled
  nothing.
- **Flux applies it, not Ansible** (D5.17, Wave C's UPS PR, which went alone). Its
  `containers_list` entry carries `owner: flux`, so `deploy.sh`, the play and the GitOps
  deployer all skip it. A merged change reaches the cluster when `promote` publishes and
  verifies the next bundle. To give it back to Ansible, follow the two-PR handback in
  `docs/flux-deploy-plane.md`; a plain revert of the flag leaves two writers on the objects.
  The Deployment is `Recreate`, so every roll stops upsd for the container start, plus a pull
  when the content tag moved. That gap is time without power-cut shutdown coverage. The flip
  itself rolled nut once, because the image ref moved to GHCR and the restart checksum
  appeared.
- **`promote` builds the image, not `k8s/image-builder`.** The renderer writes the build
  context to `DIR/images/nut/` and its content tag to `DIR/images/tags.json`, and the
  Deployment's ref reads the same tag. promote's `images` job pushes that tag to GHCR when GHCR
  does not serve it yet (`scripts/deploy_tools/ci_images.py`). So a merged edit to
  `files/entrypoint.sh`, `files/upssched-cmd` or `templates/Dockerfile.j2` moves the content
  tag, and the next promotion builds the image and rolls the pod.
- **Where the tasks went.** `tasks/main.yml` holds two includes, and no Flux path runs either.
  They stay for the handback. The `k8s/image-builder` include became promote's `images` job
  (D5.4a2) and the GHCR ref (D5.4b). The `k8s/manifests` include's `manifests_rollout: nut`
  named the Deployment for the stabilisation gate and the shared restart. The Kustomization's
  `wait` replaces the gate, and the restart checksum the renderer stamps replaces the restart.
  Its `manifests_prune: false` still keeps the `homelab/role` label off nut's objects in the
  render. Flux's own `prune: true` tracks them by the Kustomization's inventory instead, so it
  needs no label and caused no extra restart.
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

**Both nodes are armed** since 2026-08-28. daniel-server reaches upsd over the pod's
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
`# DECIDED:` marker at `FINALDELAY` in `roles/setup/nut_host/templates/host-upsmon.conf.j2`.

### Timer vs. safety net
`nut_onbatt_shutdown_delay` (300 s, `defaults/main.yml`) decides how long a blip is ridden out
before a planned stop. The end of the battery is a *different* trigger: `AT LOWBATT * EXECUTE
lowbatt` raises FSD as soon as the UPS asserts LB (`battery.charge.low` 10,
`battery.runtime.low` 120). Measured runtime 2026-08-28 was 987 s at 43 % load, so 300 leaves
roughly 11 minutes for the poweroffs. `ansible/tests/setup/test_nut_host_secondary.py` asserts the
delay stays inside that band while a host beyond `ups_host` is armed.

**Manual shutdown drill** (actually powers BOTH nodes off now that daniel-box is armed — have
console access to each): `kubectl -n homelab exec deploy/nut -- upsmon -c fsd` → host poweroff
within ~15 s (HOSTSYNC).

**The stale-ClusterIP gap is closed.** `nut_host_upsd_clusterip` is still a deploy-time
snapshot — the role asserts upsd is reachable when it runs, and nothing re-checks that address
afterwards — but `roles/setup/nut_host/templates/ups-secondary-health.sh.j2` now re-proves the link
itself every `nut_host_watchdog_interval_minutes` (10 min), from the deployed
`/etc/nut/upsmon.conf` (daniel-server: the persisted `/var/lib/homelab-secrets/etc/nut/upsmon.conf`
since H4.3c), on **every** armed host (daniel-box and `ups_host`/daniel-server both, since 2026-09). Each host pushes its own Kuma tile —
`roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2`'s `ups-secondary.json` (daniel-box,
`ups_secondary_push_token`) and `ups-secondary-daniel-server.json` (daniel-server,
`ups_secondary_daniel_server_push_token`) — deliberately never the same token: two hosts
sharing one push token would let either host's `up` satisfy Kuma's deadline and mask the
other's `down` (issue #952). daniel-box's `kuma-push.env.j2` and daniel-server's unit credential pick the right token by `inventory_hostname`.

**Ordering matters when arming the watchdog on a new host.** The Kuma tile has to exist
*before* `nut_host`'s cron starts pushing to it — deploy `uptime-kuma` first, then run
`initial_setup.yml --tags nut_host` on the host. Running the cron install first just means its
first several pushes land on a tile that doesn't exist yet; it does not risk a shared token,
because each host's token and tile are provisioned together, one leg at a time.

## Editing
- Manifests/config: `templates/*.j2` (config-secret.yaml.j2 holds all six NUT files)
- Host half, two owners. **daniel-server's is the `homelab-daniel-server` package**
  (`hosts/components/nut-secondary/`): the APC udev rule, the `nut-monitor` drop-in that points
  upsmon at the rendered `/var/lib/homelab-secrets/etc/nut/upsmon.conf`, and the
  `homelab-ups-watchdog` timer. Edit the component, and the host picks the change up through
  `homelab-updater`; no `initial_setup.yml` run applies it. **daniel-box's is the
  `ansible/roles/setup/nut_host/` role**, applied by `initial_setup.yml --tags nut_host` on
  daniel-box alone, because the role ends on `ups_host`. Both render the role's templates until
  H7.8 moves daniel-box.
