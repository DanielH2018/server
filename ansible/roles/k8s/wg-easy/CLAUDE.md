# wg-easy (k3s) — the homelab's remote-access entry point

WireGuard VPN with the wg-easy admin UI, running on daniel-box. This is the **server-side**
instance: the one whose UDP port the router forwards, and the path back in when something else
is broken. `hosts/components/pi-compose/services/wg-easy/compose.yaml.j2` documents a different instance: the Pi's
LAN-only **Docker** wg-easy, on an older version with a different auth model.

**Do not read the two as one service.** They differ in platform, version, auth and UDP port:

| | this role (k3s) | the Pi's `pi-compose` wg-easy (Docker) |
|---|---|---|
| Host | daniel-box | daniel-pi |
| Version | v15 | v14 |
| Admin auth | app's own SQLite, set via setup wizard | `PASSWORD_HASH` — and on the Pi, **unset** |
| Edge | `wg-easy.<domain>`, https, Authelia | LAN IP only, unauthenticated |
| WireGuard UDP | 51820 | 51822 |

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses `--tags=wg-easy`
  with exit 5
- **Image:** `ghcr.io/wg-easy/wg-easy` (`wg_easy_k8s_image`)
- **Route:** `wg-easy.<domain>` · `wg-easy.local.<domain>`, Authelia one_factor
- **Claim:** `wg-easy-config` (weekly -> B2 (default target))
<!-- /generated_from -->

- **Pinned `:15@sha256:…`** — `:15` keeps Renovate tracking the major so a v16 arrives as a
  deliberate PR; the digest keeps it immutable within v15.x.
- **`udp_port: 51820`** on the `containers_list` entry.
- **Flux applies it, not Ansible** (D5.12, an own-window flip with the operator on the LAN). Its
  `containers_list` entry carries `owner: flux`, so `deploy.sh`, the play and the GitOps
  deployer all skip it, and a merged change reaches the cluster when `promote` publishes and
  verifies the next bundle. To give it back to Ansible, follow the two-PR handback in
  `docs/flux-deploy-plane.md`; a plain revert of the flag leaves two writers on the objects.
  The Deployment is `Recreate` and the role declares no snapshot claim, so the tag renders no
  `-snapshot` Job. A change that rolls the pod cuts every WireGuard peer until the new pod is
  up, and this is the sole remote-access path, so a roll that fails starts the recovery from the
  LAN: read the takeover census in the flip PR, then revert the flip. The operator must not be
  connected through wg-easy when it rolls.
- **Where the task went.** `tasks/main.yml` holds one task, the include of `k8s/manifests`
  with `manifests_rollout: wg-easy`, and no Flux path runs it. It stays for the handback. The
  Kustomization's `wait` replaces the rollout wait. The orphaned `wg-easy-env` Secret that the
  task's comment names is still deleted by hand, because no prune reaches a Secret.

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

## Peer reach: the LAN, not the cluster networks
The server MASQUERADEs peer traffic out of its own pod, so a peer would arrive at any ClusterIP
or pod IP as the wg-easy pod and bypass the Authelia gate on that service's route. The
`peer-cluster-drop` init container stops that: it inserts `FORWARD -i wg0 -d <cidr> -j DROP` at
position 1 for `k3s_pod_cidr` and `k3s_service_cidr`, and logs `iptables -S FORWARD` —
`kubectl -n homelab logs deploy/wg-easy -c peer-cluster-drop` is the live proof.

- Peers keep the LAN: SSH to the nodes and the Pi, and the Traefik and DNS VIPs. Those are LAN
  addresses, and kube-proxy DNATs them in the host netns, after this pod's FORWARD chain.
- Do not narrow peers to "the Traefik VIP and DNS only". That cuts SSH, the way back in.
- The rules are rendered from the repo, never set as a PostUp in the admin UI: the app's DB is
  lost on a PVC rebuild. Why an init container is enough is the `DECIDED:` marker in
  `templates/deployment.yaml.j2`.
- Enabling per-client filtering in the UI inserts wg-easy's `WG_CLIENTS` jump above these DROPs,
  so a per-client allow rule naming a cluster CIDR would take effect.

## In-cluster reachability
Authelia gates the Traefik ingress, not the in-cluster ClusterIP path. The
`netpol-baseline` policy for this workload is what stands between an arbitrary pod and the admin
API — check `roles/k8s/netpol-baseline/templates/networkpolicy-wg-easy.yaml.j2` before assuming
the edge is the only way in.
