# traefik — the cluster ingress edge

Traefik terminates TLS and installs the IngressRoute/Middleware CRDs every other k8s
role depends on. See repo-root `CLAUDE.md` for shared conventions, and the "Where to
Look" table's note that this role must render before anything referencing its CRDs.

## At a glance
- **Deploy tag:** `--tags "traefik"`.
- **No route of its own** — an infra role; the dashboard has its own IngressRoute
  (`dashboard-ingressroute.yaml.j2`).
- **Claim:** the `acme.json` cert store (`Recreate`, ReadWriteOnce — two Traefiks
  racing to write it would corrupt it).
- **`k8s_autodeploy: false`** — platform: a failed deploy removes the ability to reach
  or observe anything else, and host probes stay green straight through that kind of
  outage. Reason is in `defaults/main.yml`.

## Notable
- **A startupProbe, not `/ping`, is what proves this pod has routers.** `/ping` answers 200
  from the moment the process starts. On 2026-09-06 the boot-time download of the CrowdSec
  bouncer plugin timed out, the `crowdsec` Middleware the https entrypoint names resolved to
  "invalid middleware type", every router on that entrypoint was rejected, and the pod sat
  3/3 Ready serving 404 to the whole fleet for 3.5 hours (#1322). Traefik does not retry the
  download. The probe targets `edge-selfcheck-ingressroute.yaml.j2`, a router of Traefik's own
  on the https entrypoint backed by `ping@internal`, so the same failure that kills every app
  route kills it too and kubelet restarts the container instead. Three constraints hold it
  together, all ENFORCED by `ansible/tests/services/test_traefik_edge_selfcheck.py`: the route
  stays on an entrypoint whose chain names the crowdsec Middleware, the probe path stays the
  route's own, and the route matches on a path rather than a Host (a kubelet probe sends no
  SNI, and Traefik answers a matched `Host()` router with 421 when SNI and Host disagree).
- An initContainer runs `chmod 600 /data/acme.json` on every start: kubelet's `fsGroup`
  handling ORs group bits into every file on the volume at mount time, which flips
  Traefik's own `0600` back to `0660` and makes it refuse to load the ACME account.
- An initContainer stages the CrowdSec image's bundled hub tree into `/etc/crowdsec`,
  owned by the pod uid, and it runs **before** the config seed below. That rsync runs as the
  pod's own uid and cannot read the root-only staged hub, so the parser configs it does
  copy — symlinks into that tree — resolved to nothing and the agent dropped
  `geoip-enrich` on every start (#1211). Ordering is the fix: after the rsync, the hub
  directory already exists owned by the pod uid, and root with `ALL` dropped cannot
  write into it.
- A second initContainer seeds the CrowdSec bouncer sidecar's config
  (`traefik_k8s_manage_crowdsec`) — `crowdsec` deploys **before** traefik in
  `containers_list` specifically so its LAPI machine credential exists before this
  sidecar starts.
- A third initContainer copies the CrowdSec image's bundled datafiles into the agent's
  data volume, world-readable. The image ships them `0600 root:root` and its entrypoint
  symlinks rather than copies them, so the non-root sidecar could not read through the
  link and GeoIP never initialised. This container is the role's only `runAsUser: 0`,
  and it reaches nothing but that volume.
- Ports are unprivileged inside the pod (`8000`/`8443`/`8082`); the Service maps the
  public `80`/`443` to them, avoiding `NET_BIND_SERVICE`. `runAsUser` is pinned to
  `traefik_k8s_uid` (65532).
