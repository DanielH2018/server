# Traefik startup: plugins, the edge probe and the init containers

`ansible/roles/k8s/traefik/CLAUDE.md` is the role doc, and it keeps the rules: the probe
exists, the bouncer's metrics ticker stays at zero, and the init containers run in the order
they are written in. This page is the working-out — the outage that bought the probe, the
measured red path, what Traefik's plugin manager does at every process start, how to change
the in-repo local plugin, and what each CrowdSec init container works around. A session reads
it when it edits the plugin, probe or sidecar wiring (#2989).

## The outage the startupProbe exists for

`/ping` answers 200 from the moment the process starts. On 2026-09-06 the boot-time download
of the CrowdSec bouncer plugin timed out, the `crowdsec` Middleware the https entrypoint names
resolved to `invalid middleware type`, every router on that entrypoint was rejected, and the
pod sat 3/3 Ready serving 404 to the whole fleet for 3.5 hours (#1322). Traefik does not retry
the download.

The probe targets `edge-selfcheck-ingressroute.yaml.j2`, a router of Traefik's own on the
https entrypoint backed by `ping@internal`, so the same failure that kills every app route
kills it too and kubelet restarts the container instead.

Three constraints hold it together, all ENFORCED by
`ansible/tests/services/test_traefik_edge_selfcheck.py`: the route stays on an entrypoint
whose chain names the `crowdsec` Middleware, the probe path stays the route's own, and the
route matches on a path rather than a Host. A kubelet probe sends no SNI, and Traefik
answers a matched `Host()` router with 421 when SNI and Host disagree.

## The red path is measured — do not re-run the outage

Measured 2026-09-10 on `traefik:v3.7.12`, with `traefik_k8s_bouncer_plugin_version: v1.7.999`
(nonexistent) and `failureThreshold: 6` deployed for four minutes:

- The pod never became Ready and the container restarted every ~30s, reaching restart 4 in
  2m10s. `deploy.sh --tags traefik` failed at the rollout wait rather than reporting green.
- `https://<podIP>:8443/.well-known/traefik-edge-selfcheck` returned 404 throughout, and every
  app router on the https entrypoint logged `invalid middleware
  "homelab-crowdsec@kubernetescrd" configuration: invalid middleware type or middleware does
  not exist` — the routing table was gone, not merely unhealthy.
- Restarts 3 and 4 each logged `Loading plugins...` followed by `unable to download plugin
  github.com/maxlerebourg/crowdsec-bouncer-traefik-plugin: error: 500` one second later. Each
  restart makes a fresh HTTP request, so the shared `/plugins-storage` emptyDir does **not**
  short-circuit recovery and no per-container-start wipe is needed.
- Recovery: reverting both values and redeploying restored a 1/1 pod with `restarts=0`,
  `selfcheck=200`, and 302/200 on the LAN routes. Total edge outage 11:53Z–11:57Z.
- Expected boot noise, not a defect: the selfcheck route has no `Host()`, so Traefik logs
  `No domain found in rule PathPrefix(...)` and falls back to the default TLSOption for it.

**Why each restart re-downloads** (#1345), from Traefik v3.7.12's own `pkg/plugins`:
`NewManager` calls `resetDirectory` on the sources root at every process start;
`RegistryDownloader.Download` always issues the GET, using an existing archive only as an
`X-Plugin-Hash` validator that a partial file fails; and `SetupRemotePlugins` calls
`ResetAll()` — which empties both the GoPath and the archives directory — on any install
failure.

## Why the bouncer reports no usage metrics

Under plugin v1.7.1 the default 600s metrics ticker intermittently stops the 60s stream ticker
for 600s or 1200s, silently, and a ban or an unban made in that window does not reach the edge
(#2752). The `DECIDED: no usage-metrics ticker` comment in `templates/dynamic.yaml.j2` has the
evidence and upstream's issue.

The ticker is a package global inside the plugin, so a Middleware edit alone does not stop a
running one. The Traefik pod has to restart, and the manifests rollout-restart does that.

## Changing the in-repo local plugin

`cloudflare-realip` is an in-repo Traefik **local** plugin (`files/cloudflare-realip/`),
projected from the `traefik-cloudflare-realip` ConfigMap to `/plugins-local/src/<module>/`. It
is deliberately not a downloaded plugin, so it adds no second startup download beside the
bouncer's, and it is gated with the bouncer so the startupProbe covers a failed load of it
too.

A name mismatch between `experimental.localPlugins`, the Middleware, the ConfigMap items and
the manifest's `import` disables every plugin at startup, and `--dry-run` cannot see that.
`ansible/tests/k8s/test_traefik_cloudflare_realip.py` pins the names.

To change the Go source, run it first under the release binary of `traefik_k8s_image`'s
version with a local `plugins-local/` tree: a Yaegi error appears only at Traefik startup, as
`Plugins are disabled because an error has occurred`.

## What each CrowdSec init container works around

Three init containers run before the agent and bouncer sidecars, and their order is
load-bearing.

1. **The hub-tree rsync** stages the CrowdSec image's bundled hub into `/etc/crowdsec`, owned
   by the pod uid, and runs **before** the config seed. That rsync runs as the pod's own uid
   and cannot read the root-only staged hub, so the parser configs it does copy — symlinks
   into that tree — resolved to nothing and the agent dropped `geoip-enrich` on every start
   (#1211). Ordering is the fix: after the rsync the hub directory already exists owned by the
   pod uid, and root with `ALL` dropped cannot write into it.
2. **The bouncer config seed** (`traefik_k8s_manage_crowdsec`) needs the `crowdsec` role's
   LAPI machine credential to exist already, which is why traefik's `containers_list` entry
   declares `depends_on: [crowdsec]`.
3. **The datafile copy** puts the image's bundled datafiles into the agent's data volume
   world-readable. The image ships them `0600 root:root` and its entrypoint symlinks rather
   than copies them, so the non-root sidecar could not read through the link and GeoIP never
   initialised. This container is the role's only `runAsUser: 0`, and it reaches nothing but
   that volume.
