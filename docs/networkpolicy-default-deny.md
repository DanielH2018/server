# Default-deny ingress NetworkPolicies

Design doc. Written 2026-08-16. Status: **slice 1 implemented and inert; not yet activated.**
The `netpol-baseline` role, the six labelled apps and the probe job are all in the tree, but
`netpol_baseline_enforced` is still `false` — the policy renders an allow-all body, so nothing
is fenced. Turning it on is a separate deploy, and slices 2–5 are still design only.

## The problem

Kubernetes admits every pod-to-pod connection by default, and a NetworkPolicy only
constrains the pods it *selects* — a pod no policy selects accepts everything, forever.
This cluster has four workload policies (`n8n-broker`, `headlamp`, `flaresolverr`,
`registry`) and **zero** with an empty `podSelector`, so the other ~50 workloads accept
connections from anything in the cluster.

Authentication and the WAF sit at the edge: Traefik → Authelia → CrowdSec. That covers
traffic *arriving from outside*. It does nothing for pod-to-pod. A compromised workload —
karakeep's headless Chrome and qbittorrent's payloads being the obvious candidates — can
dial Pi-hole, mosquitto, the registry, Longhorn's API or any app's service port directly,
never passing the edge and so never meeting Authelia or CrowdSec.

Default-deny is what turns lateral movement from "just connect" into "needs a policy
exception".

## Enforcement facts this design rests on

All measured on this cluster, not assumed.

| Fact | Evidence |
|---|---|
| **Ingress policies are enforced**; kube-router's netpol controller runs (k3s default, `--disable-network-policy` not passed) | `n8n/templates/networkpolicy.yaml.j2` header; four live policies |
| **Egress policies select pods and block nothing** | measured 2026-08-07, recorded in `sonarr/templates/isolation-probe-job.yaml.j2` |
| **Kubelet probe traffic needs no ingress rule** | `flaresolverr` admits :8191 only from prowlarr, yet is probed on :8191 and runs 1/1. Corroborated by `headlamp` (:4466 from traefik only, 1/1 for 28h) |
| **hostNetwork pods cannot be policed by podSelector** | `node-exporter` (both nodes), metallb `speaker` — pod IP *is* the node IP |
| **hostPort traffic arrives with a node IP**, needing an `ipBlock` | `registry`'s policy carries `ipBlock` entries for exactly this |
| Node bridge addresses are `10.42.0.1` (daniel-box) and `10.42.1.1` (daniel-server) | `ip -4 -o addr show cni0` on each |
| All seven LoadBalancers are **ETP=Local**, so external client IPs are preserved | `kubectl get svc -o jsonpath=…externalTrafficPolicy` |

Two consequences worth stating so nobody "fixes" a non-problem later:

- **Ingress-only.** Writing egress policies here produces something that reads like a
  control in the repo and does nothing in the cluster — worse than no policy, because it
  invites trust.
- **No pod loses cluster DNS.** Because egress is unenforced and CoreDNS is reached
  outbound, an ingress-only policy cannot break name resolution for the pod it selects.

### Correction carried into this design

An earlier draft of the baseline used `ipBlock: {cidr: 10.42.0.0/16}` to admit "the
nodes". Both pod CIDRs sit inside that /16 and `ipBlock` matches on source IP regardless
of whether the source is a pod, so that single line would have readmitted **every pod in
the cluster** — cancelling the default-deny while reading like a node allowance. The
baseline uses `/32` host addresses only.

Note also that `registry`'s policy allows `10.42.1.0/32`, which matches neither node's
`cni0` (`.1` on both). It is probably vestigial — registry runs on daniel-box, so
`10.42.0.1/32` is the entry doing the work. **Verify before copying it anywhere**; do not
propagate it on the assumption it was derived from a real observation.

## Approach: trusted-infra baseline

Three callers legitimately reach almost every pod: **Traefik** (front door for every web
UI), **Prometheus** (scrapes every workload), and **the nodes** (containerd pulls,
hostPort paths, host crons). Stating that once per namespace, rather than restating it in
~50 per-workload policies, is the whole design.

Rejected alternative — a full per-workload allowlist — is stricter only in that Traefik and
Prometheus would reach each pod on its declared port rather than any port. It costs ~50
hand-written policies, each of which must independently model the VIP hairpins, hostPort
paths and DB-configured callers the census turned up; and each miss fails silently, days
later. The baseline is a strict superset, so any individual workload can still be tightened
later without touching the other 49.

### One policy per namespace, not two

`deny-all` plus `allow-trusted-infra` is the textbook shape, but with `podSelector: {}` the
second already implies the first. One object per namespace means the off-state has one
thing to get right.

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: baseline-ingress
  namespace: homelab
spec:
  podSelector:
{% if netpol_baseline_scope == 'namespace' %}
    {}                                   # slice 5: every pod in the namespace
{% else %}
    matchLabels:
      netpol-baseline: enforced          # slices 1-4: only labelled workloads
{% endif %}
  policyTypes: [Ingress]
  ingress:
{% if netpol_baseline_enforced %}
    - from:
        - podSelector: {matchLabels: {app: traefik}}
        - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: observability}}
          podSelector: {matchLabels: {app: prometheus}}
        - ipBlock: {cidr: 10.42.0.1/32}
        - ipBlock: {cidr: 10.42.1.1/32}
{% else %}
    - {}                                # OFF: one empty rule = allow all
{% endif %}
```

### The selector migrates, it does not start wide

`podSelector: {}` fences every pod in the namespace the instant it is applied, which would make
slice 1 a whole-namespace change wearing a six-app label. The selector therefore starts as an
opt-in label (`netpol-baseline: enforced`), each slice labels its own workloads, and a final
slice switches `netpol_baseline_scope` to `namespace`.

That last switch is **slice 5, with its own PR**, and it is the risky one: at that moment every
pod nobody remembered to label gets fenced at once, including anything added between now and
then. It is gated on a check that enumerates pods in the namespace lacking the label and fails
if the list is non-empty, which turns a silent catch-all into an explicit reconciliation.

### Rollback is a variable, never a deletion

`kubectl delete` is denied and `kubectl apply` does not prune, so **removing the template
leaves the live policy enforcing** (the repo's own orphaned-objects finding). The policy is
therefore *always* rendered, and `netpol_baseline_enforced: false` renders a permissive
body. Rollback is a one-line var flip plus a deploy.

The two shapes are exact inverses and are the easiest thing here to get backwards:

- `ingress: []` — empty list — **denies everything**
- `ingress: [{}]` — one empty rule — **allows everything**

### `from` peers: OR vs AND

Separate list items are OR'd; sibling keys within one item are AND'd. This is live in
slice 1 (traefik → grafana:3000, prometheus:9090) and getting it wrong yields a policy that
admits an entire namespace while passing a negative probe:

```yaml
# WRONG — admits all of homelab, plus traefik pods in the policy's own namespace
from:
  - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: homelab}}
  - podSelector: {matchLabels: {app: traefik}}

# RIGHT — the traefik pod in homelab, and nothing else
from:
  - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: homelab}}
    podSelector: {matchLabels: {app: traefik}}
```

A bare `podSelector` is always scoped to the policy's own namespace.

### Traefik's own ports stay open

The baseline selects Traefik too. Its web ports (`:80`/`:443`) must stay reachable from all
sources, for the reason `n8n`'s policy already gives for :5678 — they are reachable from the
whole network today and narrowing buys nothing. The alternative is enumerating LAN,
WireGuard and Cloudflare CIDRs for the front door, and with ETP=Local the real client IP is
preserved, so external traffic arrives as `10.0.0.x` rather than a node IP.

Traefik's `:8080` dashboard/metrics is fenced to Prometheus instead. That is the port worth
fencing.

This also keeps the four existing `netpol-probe-job`s working: each leads with a control
that dials `traefik:80`, and that control only proves anything while traefik:80 is open to
the namespace.

## Slice plan

Each slice is its own PR carrying its own probe job. **Slice 1 is the traefik-only leaf
apps, not observability** — a deliberate swap from the rollout option as presented, for the
reason below.

| # | Scope | Why here |
|---|---|---|
| **1** | Leaf apps: bento-pdf, littlelink, speedtest, healthchecks, ical-proxy, code-server | Traefik is the only in-cluster caller of these six. Exercises the whole mechanism — baseline policy, var flip, probe job — at near-zero blast radius, and a mistake surfaces as a 502, not silence. terraria and valheim are deliberately NOT here: they are reached over their own MetalLB VIPs by game clients on the LAN, not through Traefik, so the baseline's traefik rule would not cover them and fencing them would need per-workload peers this slice does not design |
| **2** | Media stack + bridges: sonarr, radarr, prowlarr, bazarr, jellyfin, tdarr, qbittorrent, configarr, janitorr, monitor-bridge, autofix-bridge | Densest genuine app-to-app mesh; several callers are DB-configured and unprobeable |
| **3** | `observability` namespace | Four hostPort ingress paths, cross-namespace inbound from three homelab workloads, thick intra-namespace mesh |
| **4** | Infra tier: traefik, authelia, crowdsec, pihole, mosquitto, nut, registry, headlamp, n8n | Highest consequence; do it once the pattern is proven |
| **5** | Switch `netpol_baseline_scope` to `namespace` | Makes a workload fenced-by-default instead of opt-in. Gated on zero unlabelled pods |

**Why observability moved from first to third.** It is small in pod count but dense in
exactly the paths that are hardest — `loki:3100`, `tempo:3200`, `prometheus:9090` and
`otel-collector:4317` all take traffic over hostPorts (that last one is how both hosts'
Claude Code exports OTLP), plus inbound from monitor-bridge, homelab-mcp and Traefik. It is
also the instrument you would use to notice that a later slice broke something, so fencing
it first means debugging slices 2–4 with the monitoring possibly impaired.

### Slice 4 specifics

- **`pihole:53` stays open to the LAN.** It is `daniel-server`'s resolv.conf nameserver and
  serves LAN clients; its callers are hosts, not pods. Fence `pihole:80` (the admin UI) to
  Traefik. The UI is the attack surface; :53 is what takes the house down.
- **`mosquitto:1883`** is reached by zigbee2mqtt through the MetalLB VIP `10.0.0.242`, not
  the ClusterIP. With ETP=Local the source may be the pod IP or may be SNAT'd — **observe
  it before writing the selector**, and admit both the podSelector and the node `ipBlock`
  until observed. Home Assistant also reaches it, configured in HA's `.storage` and
  therefore invisible to grep.
- **Three host crons dial Traefik** from the node — `cloudflare-ip-drift.sh`,
  `crowdsec-appsec-verify.sh`, `crowdsec-update-home-allowlist.sh`. Node-sourced, so they
  need the `ipBlock` entries, not a podSelector.

## Verification, and what it does not cover

Each slice extends the existing `netpol-probe-job.yaml.j2` pattern: an inverted Job that
**succeeds when a connection fails**, leading with a control that dials `traefik:80` so a
failure is attributable to the policy rather than to DNS or a pod with no network.

### The liveness target must be an unauthenticated route

A probe's liveness assertion — the one that proves the target app is actually up, so that
"unreachable" cannot be mistaken for enforcement — must target a route with
`use_authelia: false`. Authelia's `access_control` has no bypass rule for
`*.local.<domain>`, so on an Authelia-gated route the forwardauth middleware answers an
unauthenticated request *before* Traefik proxies to the pod.

Both outcomes are wrong: a 401 makes the probe permanently red for a reason unrelated to
enforcement, and a 302-to-portal prints a passing liveness result without ever contacting
the app — which restores the exact false-pass the four-assertion design exists to
eliminate. Slice 1 uses `littlelink` (`use_authelia: false`, hostname `www`, port 3000) for
this reason. Later slices must pick their own unauthenticated target rather than copying
the app name. This is the same trap already recorded in this repo as "an Authelia 302
doesn't prove the backend was reached."

Positive-path probes are new work — they need a pod carrying the *caller's* labels, which
the existing inverted jobs do not require.

**Three caller classes cannot be probed at deploy time.** These are the silent-failure set,
and they are why the slices are staged rather than landed together:

1. **VIP hairpins** — homepage's widgets reach ~8 services via
   `https://<svc>.local.<domain>`, exiting to the Traefik VIP and re-entering as
   traefik → callee. The callee's rule needs `app: traefik`, not `app: homepage`.
2. **Host crons** — the three above; not running at deploy time.
3. **CronJob and Job callers** — configarr → sonarr/radarr, janitorr →
   sonarr/radarr/jellyfin, pi-peer-backup, the image-builder Jobs.

Callers configured in application databases rather than manifests are invisible to the
census and belong to the same set: sonarr/radarr → `wireguard:8080` and `prowlarr:9696`,
bazarr → sonarr/radarr, freshrss → feed-cache, home-assistant → mosquitto.

## Where the code goes

- Namespace baselines: a new role `ansible/roles/k8s/netpol-baseline/`, placed in
  `containers_list` **after traefik** (CRDs) and before the workloads. It genuinely belongs
  to no single service; precedent for a non-service-owned policy is
  `setup/k3s/files/longhorn-metrics-networkpolicy.yaml`.
- Per-workload policies: `ansible/roles/k8s/<name>/templates/networkpolicy.yaml.j2`,
  alongside the four that already exist.
- Probe jobs: `ansible/roles/k8s/<name>/templates/netpol-probe-job.yaml.j2`, same pattern.

## Answers from slice 1

Both blocking items were settled by observation on 2026-08-17, after slice 1 was deployed and
enforced. Slice 2 depends on the first; slice 4's mosquitto rule depends on the second.

### 1. `registry`'s `10.42.1.0/32` is vestigial — but do not simply delete it

`kubectl -n homelab get pod -l app=registry -o jsonpath='{.items[0].spec.nodeName}'` → **daniel-box**,
whose podCIDR is `10.42.0.0/24`. Registry serves containerd over a hostPort on the node it runs on,
so `10.42.0.1/32` is the entry doing the work. `10.42.1.0/32` matches neither node's `cni0` (both
are `.1`); it is the network address of daniel-server's CIDR, not an address anything sources from.

The correct repair in slice 4 is to **replace** it with `10.42.1.1/32`, not to drop it. Registry is
not pinned to a node, so if it is ever rescheduled onto daniel-server the working entry becomes the
one that is currently wrong.

### 2. Pod → MetalLB VIP traffic is SNAT'd to the node bridge address, despite ETP=Local

Measured against the live mosquitto:

- zigbee2mqtt's pod IP: `10.42.0.242` (on daniel-box)
- every line in mosquitto's log: `New connection from 10.42.0.1:<port> on port 1883`

`10.42.0.1` is daniel-box's `cni0`. **ETP=Local preserves the source IP for traffic arriving from
outside the cluster, not for a pod dialling its own cluster's LoadBalancer VIP** — that path
hairpins through the node and is masqueraded. No pod IP appears in the log at all.

Consequence for slice 4: **a `podSelector` cannot express "mosquitto accepts zigbee2mqtt".** The
rule must use the node `ipBlock`s, which also means it cannot distinguish zigbee2mqtt from any
other pod on the same node — so fencing mosquitto by source is not achievable while the client
dials the VIP. Either accept that, or move the client to the ClusterIP so pod identity survives.

The baseline's existing `10.42.0.1/32` and `10.42.1.1/32` entries already admit this path, so any
workload fenced by the baseline remains reachable over a VIP hairpin today.

## Open items still outstanding

1. Whether a lint should require every `containers_list` entry to have a policy — the
   baseline makes a missing policy safe rather than broken, so this is optional, but it is
   the executable-check end of the repo's escalation ladder. Slice 1 shipped a narrower
   version of this: `ansible/tests/test_netpol_baseline_labels.py` pins the labelled set.
2. The probe's `activeDeadlineSeconds` (120s) is shorter than the Ansible wait (180s), so a
   deadline-exceeded run produces a misleading diagnosis in either branch of the fail message.
   Slice 1's probe completed well inside both, so the real timings are now known and this can
   be tuned rather than guessed.
