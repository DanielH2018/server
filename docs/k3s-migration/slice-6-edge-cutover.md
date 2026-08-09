# Slice 6 — Edge Cutover: CrowdSec, Pi-hole, Router → VIP

**Status — 2026-08-09 (evening). A1 partial + B1 EXECUTED.** A1's automatable half is done:
daniel-box's upstream DNS is pinned by a resolved drop-in (`5ee81f3d` — it was DHCP luck
before), daniel-server's resolv.conf primary is a role default ready for the B3 flip. Still
open from A1, both needing the operator: the cold-boot gate (reboots both hosts — pick a
maintenance evening) and the router-UI reads (DHCP DNS option value, forward table).

B1 is live and gated (`c6ffcae5` + fixes `2163616d`/`510f71b9`/`d43ddc3d`): engine
(LAPI+AppSec, v1.7.8 pinned, DB PVC) + traefik agent sidecar tailing a real file access log
+ bouncer plugin on both entrypoints in stream mode. The enforcement gate
(`deploy.yml --tags b1-gate`) proved ban → 403-at-VIP → unban → resume, from the Pi itself.
Three rollout lessons, each now encoded in comments where it bit: traefik DISABLES plugins
(rather than failing) when readOnlyRootFilesystem blocks /plugins-storage — every router
referencing the middleware 404s and the edge goes dark; the crowdsec image refuses to start
without a /var/lib/crowdsec/data volume even agent-side; and the kernel's 128
inotify-instances-per-uid budget is shared by every root process on the node — the sidecar
was the straw (now 1024 via the k3s role). Bonus fix: the http→https redirect had been
emitting the container-side :8443 into Location since slice 1; now pinned to :443.

**Status — 2026-08-09 (late afternoon). B2 EXECUTED except the dashboard.** One LAPI:
daniel-server's engine is demoted to agent-only (`78fccfd2` + fixes), its traefik bouncer
polls the cluster LAPI over TLS via `crowdsec-lapi-k8s.local.<domain>` (D3 IngressRoute,
`5cf7d626`), and the two-edge gate PASSED — one `cscli decisions add` 403s at the k8s VIP
*and* the Docker edge, then lifts (`0f3f6433`). The home-allowlist updater followed the LAPI
to daniel-box (`075d24d5` — `cscli allowlists` is LAPI-machine-only, proven by the deploy
failure), pushing its Kuma monitor directly; verified live (state files written after a
successful sync). Cluster Prometheus scrapes the engine's :6060.

Three plugin traps found live, each encoded where it bit: with `crowdsecLapiScheme: https`
the plugin requires an explicit CA (it ignores system CAs) or the middleware fails and every
router 404s — the PUBLIC edge went dark for ~10 min; `CrowdsecAppsecScheme` silently
inherits the LAPI scheme, breaking the plaintext local AppSec listener (fail-open, so the
only symptom is a log line); and plugin middleware config does NOT reliably hot-reload
through the directory-mounted dynamic config — a traefik restart is part of any bouncer
config change. Also of note: the old `crowdsec_bouncer_api_key` and the Docker LAPI's DB
(crowdsec-db volume) are retained for rollback until the slice closes.

**B2 COMPLETE — 2026-08-09 (evening).** The dashboard moved into the engine pod (`0d185f0d`):
Metabase reads the live decision DB off the shared PVC, seeded by an initContainer (the same
wget+unzip the Docker image baked at build time), Authelia-gated at
`crowdsec-k8s.local.<domain>` — verified 302 through the ingress with DNS resolving to the
VIP. The Docker container is stopped and removed, its role archived, daniel-server's count is
26, and the Kuma monitor follows.

Verified end-state: **one LAPI, three agents** (`localhost` in-pod, `k8s-traefik-agent`
sidecar, `daniel-server-agent` — all three authenticating, confirmed both by
`cs_lapi_machine_requests_total` and by `cscli lapi status` on daniel-server naming the
cluster URL); both bouncers enforcing from it (two-edge gate passed); cluster Prometheus
scraping the engine (`up{job="crowdsec"}=1`); the allowlist cron writing state on daniel-box
with its tombstones applied on daniel-server (cron gone, state dir gone). Note the Docker
Prometheus still scrapes its local agent as `crowdsec_daniel-server` — correct, that agent
still exists; the LAPI/decision series now come from the cluster job.

**Cluster SSO detection gap closed — 2026-08-09 (`d1a67d08`).** The k8s Authelia logged only
to stdout, so nothing fed `LePresidente/authelia` for the cluster edge: every `-k8s` service's
login page sat behind an unmonitored portal. It now writes a file log (keep_stdout preserved)
with its own agent sidecar — **one LAPI, four agents**, all four confirmed on
`cs_lapi_machine_requests_total`. Two container-security lessons, applied to the traefik pod
too since its shape is identical: the log file must EXIST before the agent starts (crowdsec's
file acquisition never retries a missing file — one error and that datasource is dead for the
container's life, leaving a healthy-looking pod with no detection; traefik escaped only by
crash-looping into a later start), and neither agent runs as root any more (root with ALL caps
dropped has no DAC_OVERRIDE, so it cannot write the init container's `/etc/crowdsec` tree and
`cscli parsers install` dies on mkdir — as the pod's own uid each agent owns its config and log
outright, and needs no privilege to read a file and post to the LAPI).

**Next:** B3's build half is done (2026-08-09) — the cluster Pi-hole serves on 10.0.0.243 and
every build gate passed. Its cutover half, and B4/B5, wait on the A1 cold-boot gate and the
router-UI reads — both operator tasks.

> design.md row: *"Edge cutover: CrowdSec LAPI, Pi-hole, router forward → VIP, flip the four
> `*_host` flags — exit: external access and LAN DNS on the new path."*

This slice makes the cluster the front door. Everything before it moved services *behind* an
edge that stayed put on daniel-server (10.0.0.161); this slice moves the edge itself: the
router's 80/443 forward goes to the MetalLB ingress VIP (10.0.0.240), LAN DNS moves in-cluster
behind its own VIP, and CrowdSec — engine, LAPI, AppSec, dashboard — reassembles around the new
edge. Two of design.md's assumptions did not survive contact with the measured baseline and are
revised in D5/D6.

---

## Baseline — measured 2026-08-09

| What | Measured |
|---|---|
| Router forwards | 80/443 + 51820/udp → 10.0.0.161 (design.md §7; router UI is the source of truth, re-confirm before B5) |
| Pi-hole DHCP | **`[dhcp] active = false`** in the live `pihole.toml` on daniel-server — the router serves DHCP; Pi-hole is DNS-only despite the published :67 and NET_ADMIN in the role |
| daniel-box node DNS | systemd-resolved stub → upstream 75.75.75.x (ISP, DHCP-supplied) — does **not** traverse Pi-hole; cold-boot-safe today by accident, not by configuration (no Ansible owns it) |
| daniel-server node DNS | plain-file `/etc/resolv.conf`: `10.0.0.161` (itself/Pi-hole) then `1.1.1.1` — written once by the pihole role (tasks 88-102) |
| CrowdSec engine | lives inside the *traefik* role's compose (crowdsec + AppSec :7422), one bouncer total: the Traefik plugin (`crowdsec:8080`, stream mode), applied entrypoint-wide on http+https; `roles/containers/crowdsec` is only the Metabase dashboard |
| CrowdSec acquisition | daniel-server-local files: traefik access.log, /var/log/auth.log, /var/log/authelia/*.log, AppSec :7422. daniel-pi connects to nothing — there is exactly one agent in the system |
| Cluster edge today | k8s Traefik on VIP 10.0.0.240, `externalTrafficPolicy: Local`, **no CrowdSec anything** (deliberate — static-config.yaml.j2:43), access log to stdout, `k8s_public_route: false` keeps it private by missing Host rules, not by DNS |
| Strangler bridge | daniel-server traefik forwards every `bridge_hostname` to the cluster; **no reverse path exists** (cluster → daniel-server is only ssh-seeding, Kuma pushes, MQTT/NUT — no HTTP proxy) |
| MetalLB | 10.0.0.240 ingress (own autoAssign:false pool), 241 jellyfin-lan, 242 mosquitto; pool runs to .250 |
| wg-easy | Docker on daniel-server, 51820/udp published 0.0.0.0, hands out `WG_DEFAULT_DNS: 10.0.0.161` to clients |
| cloudflare-ddns | already in k8s; publishes the WAN IP (apex proxied + terraria direct) — ingress-host-agnostic, **no slice-6 change** |

## Decisions

### D1 — LAPI lives in the cluster; daniel-server keeps an agent, not an engine

The public edge moves to the cluster, so decisions must be enforced there with local latency:
LAPI + a log-processing agent + AppSec run as one in-cluster workload (`roles/k8s/crowdsec`).
daniel-server still originates signals the cluster cannot see — /var/log/auth.log, the Docker
Authelia's logs, and its traefik's access log (which keeps carrying LAN + reverse-bridged
traffic) — so its existing crowdsec container is *demoted to agent-only*, registered against
the cluster LAPI over LAN. One LAPI, two agents, decisions shared. daniel-pi connects to
nothing today and that status quo is out of scope (noted in Unverified).

### D2 — Cluster acquisition reads the Traefik file log on a shared volume, not /var/log/pods

The k8s Traefik logs to stdout today. Parsing kubelet's `/var/log/pods` symlink farm couples
CrowdSec to node paths and CRI log framing; instead Traefik gains a file access-log on a small
shared PVC (or emptyDir + sidecar-tail into the agent — decide by whichever the existing
traefik role absorbs with the smaller diff). The Docker parser configs carry over unchanged,
which keeps scenario behaviour comparable across the cutover.

### D3 — LAPI is exposed to LAN agents/bouncers via a dedicated IngressRoute, no Authelia

The daniel-server agent and its Traefik bouncer plugin authenticate with machine keys, so the
LAPI hostname (`crowdsec-lapi-k8s.local.<domain>` → 10.0.0.240) is TLS-terminated but **not**
SSO-gated, excluded from rate-limit the same way the bridge client IP already is. The Metabase
dashboard moves with the LAPI (it reads the engine DB) and *stays* Authelia-gated as today.

### D4 — Pi-hole is DNS-only on its own VIP (10.0.0.243), Unbound rides in the pod

DHCP measured inactive (see Baseline), so the broadcast problem design.md worried about does
not exist — the router keeps DHCP and only its *DNS option* changes, which also gives a free
strangler: both Pi-holes serve simultaneously while leases renew onto the new VIP. Unbound
moves as a second container in the same pod (localhost upstream, mirroring `pihole_internal`).
The dnsmasq template (wildcard + per-service overrides + `daniel-pi.lan`) renders into a
ConfigMap from the same source template. UDP+TCP :53 on VIP 10.0.0.243 from the auto pool.

### D5 — The four `*_host` flags do NOT flip here (design.md superseded)

Evidence, flag by flag: `monitoring_controller_host` pins where Kuma *pushes land* — Uptime
Kuma is still Docker on daniel-server, so flipping it breaks every health push.
`backup_controller_host`'s only consumer pulls Pi peer configs into Kopia scope — Kopia stays
on daniel-server. `portainer_manager_host` feeds the Pi's agent firewall — Portainer hasn't
moved (or dissolved). `renovate_notify_host` is arbitrary and riding along changes nothing.
All four flip (or dissolve) in slice 7 with their anchors. The design.md row predates the
slice-0 decision to keep daniel-server off the cluster until slice 7.

### D6 — 51820/udp does not flip; wg-easy stays where it is

wg-easy is in design.md's pinned set *because* it is the remote-access lifeline. The router
flip in B5 touches 80/443 only; 51820 keeps pointing at 10.0.0.161 until slice 7 decides
daniel-server's residual role. Its client DNS does change (B3) to the new Pi-hole VIP.

### D7 — LAN DNS wildcard stays pointed at 10.0.0.161 through this slice

After B4 a reverse bridge makes every Docker service reachable *through* the cluster, so the
wildcard `*.local.<domain>` COULD move to the VIP — but that adds a second hop to every LAN
request for zero resilience gain while daniel-server still hosts the services themselves. The
wildcard moves in slice 7 when the services do. What this slice changes is only the public
path. (The per-`-k8s`-service overrides already point at 10.0.0.240 and are unaffected.)

### D8 — Node DNS is made boring *before* anything moves (the A-steps)

An Ansible-owned resolved drop-in pins daniel-box's upstream (currently true only by DHCP
accident) so a router or lease change can never quietly point the node at the Pi-hole VIP and
recreate the cold-boot loop. daniel-server's resolv.conf gains the new VIP as primary with
1.1.1.1 retained as fallback — it is not a cluster node, so the "never the VIP" rule doesn't
bind it, but the fallback is what keeps its ~27 Docker services resolving when the cluster is
down. Both changes land in A1, before any B-step.

## Steps

### A1 — Prerequisites (no service moves)

- Ansible-manage node DNS per D8 (new tasks in `setup/k3s` for daniel-box; extend the pihole
  role's resolv.conf handling for daniel-server).
- **Cold-boot gate (design.md:182):** with the cluster stopped and Docker Pi-hole stopped,
  cold-boot both hosts; both must reach a registry and come up clean. Run it on a maintenance
  evening — this is the gate that licenses B3, and failing it just means Pi-hole stays Docker.
- Confirm at the router UI: DHCP scope + its DNS option value; the 80/443/51820 forward table.
- Confirm the systemd-resolved stub (127.0.0.53:53) coexists with a *VIP* :53 — it binds the
  node addresses, not 10.0.0.243, so no conflict is expected; verify, don't assume.

### B1 — CrowdSec stands up in the cluster (shadow)

New `roles/k8s/crowdsec`: engine+LAPI+AppSec, PVC for the DB, parser/scenario collections and
the Discord notification config carried from the traefik role's files. k8s Traefik gains the
bouncer plugin (same pinned version) on both entrypoints pointing at the in-cluster LAPI, and
the file access-log of D2 feeds the agent. The Docker engine keeps protecting the real public
edge untouched — the two LAPIs run in parallel and share nothing, which is fine while the
cluster edge is LAN-only.

**Gates:** `cscli lapi status` green in-pod; `cscli decisions add --ip <test-ip>` blocks that
IP at the VIP within the stream interval and unblocks on delete; AppSec answers on its port;
scenario list matches the Docker engine's.
**Rollback:** delete the role's manifests; the Docker edge never depended on any of it.

### B2 — One LAPI: daniel-server demotes to agent, dashboard moves

Re-point the daniel-server crowdsec container to agent-only mode against
`crowdsec-lapi-k8s.local.<domain>` (D3), re-register its machine key (SOPS), re-point the
Docker traefik's bouncer plugin at the same LAPI with a fresh bouncer key, then retire the
local LAPI. Move the Metabase dashboard role to `roles/k8s/` (archive the Docker one). Discord
notifications now originate from the cluster LAPI only — verify the webhook fires once, not
twice or zero times.

**Gates:** decisions raised from a daniel-server-local scenario (e.g. an ssh brute probe hitting
auth.log) appear in the cluster LAPI and are enforced by *both* Traefiks; dashboard renders
against the new DB; Kuma monitor for crowdsec repointed and green.
**Re-plumb inventory:** `crowdsec` entries in host_vars (dashboard → daniel-box k8s), Kuma
monitors, prometheus scrape of :6060 (now a cluster target), secret_rotation entries for the
new machine/bouncer keys.
**Rollback:** the Docker engine's LAPI config is one compose revert away; keys for the old
topology stay in SOPS until the slice closes.

### B3 — Pi-hole (+Unbound) to the cluster, router DNS option flips

**Build half executed 2026-08-09** (`roles/k8s/pihole`, coexisting per D4 — safe before the
gate because nothing resolves through the VIP until the DHCP option flips). NOT seeded — see
the resolved Unverified item: blocklist state is declared in the role defaults and reconciled
into gravity.db; everything else in `etc-pihole` proved env-derived or regenerable
(pihole.toml diffed clean against the FTLCONF env). The dnsmasq records render from the now-
shared template (`ansible/templates/pihole-dnsmasq.conf.j2`), wildcard pinned to
daniel-server per D7; Unbound rides the pod on :5335 (FTL owns :53 in the shared netns).
Build gates all passed via 10.0.0.243: the `-k8s` override, the wildcard, `daniel-pi.lan`,
an external name through Unbound (DNSSEC: `fail01.dnssec.works` SERVFAILs), both blocklist
tiers sinkhole (regex + big.oisd), and the Authelia-gated UI at `pihole-k8s` 302s to SSO.

**Cutover half — gated on A1's cold-boot pass.** Order: wg-easy `WG_DEFAULT_DNS` → .243 →
daniel-server resolv.conf per D8 → router DHCP DNS option → .243. The Docker Pi-hole keeps
serving 10.0.0.161:53 while leases drain; retire it only when its query log flatlines (days,
not minutes — lease time decides).

**Gates:** dig via .243 resolves a `-k8s` override, the wildcard, `daniel-pi.lan`, and an
external name (through Unbound) correctly; a VPN client resolves `*.local.<domain>` after
reconnect; ad-blocking verified (a known-blocked domain returns the sinkhole answer); cluster
cold-restart leaves both nodes reachable (A1's guarantee, re-checked once DNS is live).
**Re-plumb inventory:** pihole host_vars entry → daniel-box k8s; Kuma DNS monitor → .243;
homepage widget; the pihole role's resolv.conf tasks (now daniel-server points *away* from
itself); `probe.py pi` paths if any assume the Docker container.
**Rollback:** router DNS option back to 10.0.0.161 (single UI change), resolv.conf revert,
Docker copy is still running — nothing was destroyed until the drain-and-retire step.

### B4 — Reverse bridge + public Host rules on the cluster edge

Mirror of the existing strangler bridge, opposite direction: the k8s Traefik gains generated
routers for every `platform: docker` service on daniel-server (from hostvars, same idiom as
`config.yml.j2`'s bridge block), forwarding to https://10.0.0.161 with SNI/Host preserved so
the wildcard cert verifies; Authelia for those services keeps being applied where it lives
today (the daniel-server edge — second hop). Flip `k8s_public_route: true` so public Host
rules exist. Nothing external reaches it yet — the router still forwards to .161 — so this is
fully testable from the LAN against the VIP.

**Gates:** curl against 10.0.0.240 with a Docker service's public Host resolves, redirects to
SSO, and serves after auth; same for a k8s service; CrowdSec test ban blocks both paths.
**Rollback:** unflip `k8s_public_route`, drop the reverse-bridge template — additive change,
no consumer yet.

### B5 — The router forward flips; the old bridge comes down

Change 80/443 forwarding 10.0.0.161 → 10.0.0.240 at the router (manual; verify from LTE, not
LAN). After a soak (Kuma green across every public monitor, CrowdSec seeing external traffic,
Authelia sessions surviving on both stacks), remove the now-dead forward bridge from the
daniel-server traefik config, delete the `bridge_hostname` keys, and rework
`test_strangler_bridge.py` to its slice-6 end-state (slice-2 doc:165 anticipated: "slice 6
removes every bridge at once") — its inverse assertion becomes: every public name is either a
cluster route or a reverse-bridge route, and no `bridge_hostname` remains.

**Gates:** from LTE: a k8s service and a Docker service both load through SSO; wg-easy still
connects (51820 untouched); CrowdSec decisions fire on real internet noise within the first
hours; certificate renewals unaffected (same CF DNS-01, unchanged).
**Rollback:** router forward back to .161 — the Docker edge remains fully assembled until the
bridge-removal commit, which is the point of no easy return and therefore lands last, after
the soak.

## Exit criteria

1. External HTTPS enters at 10.0.0.240: router forwards there, CrowdSec (cluster LAPI) and
   Authelia enforce on the path, and a Docker-hosted service is reachable from the internet
   through the reverse bridge.
2. LAN DNS is served in-cluster at 10.0.0.243 by Pi-hole+Unbound; the router's DHCP hands it
   out; the Docker Pi-hole is retired with its query log observed flat first.
3. One CrowdSec LAPI (in-cluster), two agents (cluster + daniel-server), both Traefik bouncers
   enforcing from it; dashboard and Discord notifications live; old LAPI retired.
4. Both hosts cold-boot with the cluster down and reach a registry (A1 gate, re-verified after
   B3).
5. Guard tests reflect the end-state (strangler-bridge test reworked, platform counts updated);
   Kuma green across repointed monitors; no `*_host` flag changed (D5).
6. wg-easy untouched on 51820 → 10.0.0.161 (D6); LAN wildcard DNS still → 10.0.0.161 (D7).

## Unverified — resolve during execution, not by assuming

- **Router DHCP DNS option value** — assumed 10.0.0.161; read it in the router UI at A1.
- **Cloudflare records per public name** — the k8s ddns manages apex+terraria only; confirm
  the wildcard/CNAME situation covers every `use_authelia` public hostname before B4's
  `k8s_public_route` flip makes them answerable.
- **Bouncer plugin behaviour against a remote LAPI over TLS** — the plugin config grows
  `crowdsecLapiScheme: https`; verify stream mode + AppSec URL both accept the ingress
  hostname before B2 commits daniel-server to it.
- **Seeding Pi-hole state vs regenerating** — RESOLVED 2026-08-09, against the lean:
  regenerate. seed-volume verifies against a quiescent source and pihole-FTL.db moves on
  every DNS query, so a coexistence seed can never pass verification. Measured the actual
  durable state instead: one enabled adlist (big.oisd.nl), the default StevenBlack list
  disabled, two regex denies, and a stale client row from the old 192.168.50.x subnet
  (dropped). All declared in `roles/k8s/pihole/defaults` and reconciled idempotently — and
  the claim is `longhorn-nobackup` as a result.
- **AppSec CPU cost in-cluster** — it ran uncapped-ish on daniel-server; set requests/limits
  from the Docker copy's cAdvisor history at B1.
- **daniel-pi has no CrowdSec coverage today** — unchanged by this slice; candidate for a
  third agent afterwards, not scope creep now.
- **k3s stub-resolver interaction with a :53 VIP** — expected non-conflict (stub binds
  127.0.0.53, kube-dns binds cluster IPs), but A1 verifies on the live node.
