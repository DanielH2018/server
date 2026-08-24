# wg-easy (k3s) — the homelab's remote-access entry point

WireGuard VPN with the wg-easy admin UI, running on daniel-box. This is the **server-side**
instance: the one whose UDP port the router forwards, and the path back in when something else
is broken. Written 2026-08-24; the role had no `CLAUDE.md`, and the only wg-easy doc in the tree
was `roles/containers/wg-easy/CLAUDE.md`, which describes the retired daniel-server **Docker**
instance and a different auth model.

**Do not read the two as one service.** They differ in platform, version, auth and UDP port:

| | this role (k3s) | `roles/containers/wg-easy` (Docker) |
|---|---|---|
| Host | daniel-box | daniel-pi |
| Version | v15 | v14 |
| Admin auth | app's own SQLite, set via setup wizard | `PASSWORD_HASH` — and on the Pi, **unset** |
| Edge | `wg-easy.<domain>`, https, Authelia | LAN IP only, unauthenticated |
| WireGuard UDP | 51820 | 51822 |

## At a glance
- **Image:** `ghcr.io/wg-easy/wg-easy:15@sha256:…` — `:15` keeps Renovate tracking the major so a
  v16 arrives as a deliberate PR; the digest keeps it immutable within v15.x.
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`, `platform: k8s`,
  `use_authelia: true`, `udp_port: 51820`.

## The traps, all of which are written out at their own lines in the role

Read the source comments rather than trusting a summary — this section exists to tell you they are
there, because a reader who has only seen the Docker role's doc will not expect them.

- **`INSECURE=true` is correct here and is not a downgrade of the external route.** Traefik
  terminates TLS at the edge and proxies plain HTTP to the pod, so without it v15 refuses the
  request — including the first-boot setup wizard. The external route is still https and
  Authelia-fronted. See `templates/deployment.yaml.j2`.
- **The admin credential is NOT in SOPS.** v15 owns it in a SQLite DB under `/etc/wireguard`,
  created once through the setup wizard. Rotation is `cli db:admin:reset` inside the pod, which
  is why `ansible/secret_rotation.yml` no longer carries `wg_easy_password_hash`.
- **A rebuild from an empty volume is a MANUAL step, not a redeploy.** The wizard is the only
  v14→v15 import path, the CLI has no import command, and the `INIT_*` unattended variables skip
  the wizard *and* the import together — minting a fresh server keypair that invalidates every
  existing client config. Losing or recreating the PVC therefore also drops the pod back to an
  open first-boot wizard, where whoever reaches it first sets the admin password.
- **IPv6 is deliberately off.** kubelet's allowed-unsafe-sysctls covers only
  `net.ipv4.conf.all.src_valid_mark` and `net.ipv4.ip_forward`; the `net.ipv6.*` sysctls upstream's
  compose sets are excluded on purpose. Turning v15's IPv6 support off was chosen over widening
  that allow-list, which would be a k3s server-arg change plus a restart of both nodes — on the
  sole remote-access path. The UI still *displays* IPv6 addresses it will not route.
- **Client-facing settings live in the app's DB, not in this repo.** `WG_HOST`, `WG_PORT`,
  `WG_DEFAULT_DNS` and the keepalive were all v14 environment keys and were removed at the
  upgrade. `container_item.default_dns` in host_vars stays only as the record of what the value
  should be; nothing renders it any more.

## In-cluster reachability
Authelia gates the Traefik ingress, not the in-cluster ClusterIP path. The
`netpol-baseline` policy for this workload is what stands between an arbitrary pod and the admin
API — check `roles/k8s/netpol-baseline/templates/networkpolicy-wg-easy.yaml.j2` before assuming
the edge is the only way in.
