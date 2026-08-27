# Default-deny ingress NetworkPolicies

Design doc. Written 2026-08-16. Status: **all five slices deployed and enforcing** (slice 2 on
2026-08-17, slice 3 on 2026-08-19, slices 4, 4.5 and 5 on 2026-08-20). `netpol_baseline_enforced`
and `netpol_baseline_obs_enforced` are both `true`, and `netpol_baseline_scope` is `namespace` —
so every pod in `homelab` and `observability` is fenced by default, and the only way out is the
`netpol-baseline-exempt` label. The exempt set is declared in
`ansible/roles/k8s/netpol-baseline/defaults/main.yml` (`netpol_baseline_exempt_workloads`), each
entry carrying its reason inline, and a cluster-side exact-set gate refuses the deploy if the live
set drifts from it. It is deliberately not enumerated here — this sentence named four workloads
from 2026-08-17 until 2026-08-23, by which point karakeep-chrome had made it five.

Counts are deliberately not recorded here; they went stale within a day when they were. For live
ones:

```bash
kubectl -n homelab get pods -l netpol-baseline=enforced --no-headers | wc -l   # fenced pods
kubectl -n homelab get pods -l netpol-baseline-exempt --no-headers             # the exempt set
ls ansible/roles/k8s/netpol-baseline/templates/networkpolicy-*.yaml.j2 | wc -l # policies
```

See "Answers from slice 1" through "What slice 5 decided" below for what each deploy settled.

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
exception."

## Enforcement facts this design rests on

All measured on this cluster, not assumed.

| Fact | Evidence |
|---|---|
| **Ingress policies are enforced**; kube-router's netpol controller runs (k3s default, `--disable-network-policy` not passed) | `n8n/templates/networkpolicy.yaml.j2` header; four live policies |
| **Egress policies select pods and block nothing** | measured 2026-08-07, recorded in `sonarr/tasks/main.yml` (probe retired 2026-08-17; prose is the only record) |
| **Kubelet probe traffic needs no ingress rule** | `flaresolverr` admits :8191 only from prowlarr, yet is probed on :8191 and runs 1/1. Corroborated by `headlamp` (:4466 from traefik only, 1/1 for 28h) |
| **hostNetwork pods cannot be policed by podSelector** | `node-exporter` (both nodes), `metallb` `speaker` — pod IP *is* the node IP |
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
nodes." Both pod CIDRs sit inside that /16 and `ipBlock` matches on source IP regardless
of whether the source is a pod, so that single line would have readmitted **every pod in
the cluster** — cancelling the default-deny while reading like a node allowance. The
baseline uses `/32` host addresses only.

Note also that `registry`'s policy allows `10.42.1.0/32`. That is **not** a `cni0` address —
`cni0` is `.1` on both nodes — but it is not a stray either: it is daniel-server's
**`flannel.1`** address, the VXLAN interface (daniel-box's is `10.42.0.0`). Traffic genuinely
sources from it. **Verify what it is doing before copying it anywhere**, and equally before
removing it; see "Answers from slice 1" below.

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

That check catches *unlabelled* pods. It does not catch two more consequences of the same
`podSelector: {}` switch, found while building slice 3, where the target already carries some
other label and so would pass that check while still breaking:

- **`podSelector: {}` fences each probe's control target, not the probe pod itself.** Every probe
  leg in this repo is **outbound** (`netpol-probe-job.yaml.j2:44,61,104`;
  `netpol-probe-slice2-job.yaml.j2`; `netpol-probe-slice3-job.yaml.j2:72,93,102`;
  `prowlarr/netpol-probe-job.yaml.j2:53,64`; `sonarr/tasks/main.yml`) — nothing needs a
  probe pod to be reachable *inbound*. An Ingress-only policy selecting a probe pod governs what
  reaches it, not what it can reach, and return traffic rides conntrack, so being selected as a
  target cannot break an outbound assertion. What actually breaks is the mirror image:
  `podSelector: {}` also selects each probe's **control target**. `netpol-probe-slice3-job.yaml.j2:72`
  dials `traefik:80` for its control; under a namespace-scope baseline, traefik's own ingress policy
  would admit only `app: traefik`, observability's `prometheus`, and the two cni0 `/32`s — the probe
  pod matches none of those, the control fails, and the Job exits 1 with `CONTROL FAILED`, not with
  a failed inverted assertion. `netpol-probe-job.yaml.j2:51-55` already documents the same shape for
  its own NEGATIVE CONTROL leg (dialing homepage): "by construction there is no unfenced pod left"
  once slice 5 lands. Slice 5 must apply the same fix to every probe's control leg: admit the probe
  pod as an explicit peer **on its control target's policy** — the shape
  `prowlarr/templates/networkpolicy-prowlarr.yaml.j2:24-27` already uses, where prowlarr's own policy admits
  `flaresolverr-netpol-probe` as a caller because that probe's control leg dials `prowlarr` before
  asserting flaresolverr is unreachable.

  **Superseded — this half of the bullet no longer holds.** The premise is that traefik's policy
  admits only `app: traefik`, `prometheus` and the two cni0 `/32`s. Slice 4 then gave traefik an
  open-port rule, written after this paragraph. Read live 2026-08-20, its first ingress rule is
  `{"ports":[{"port":8000},{"port":8443}]}` with **no `from:`** — open to every source. So every
  control leg dialing `traefik:80` survives the flip untouched: slices 1-4 and headlamp's probe on
  the open-port rule, slice 4.5 and the sentinel legs of slices 1-2 on the sentinel policy, and
  prowlarr's on the explicit peer this bullet describes. Slice 5 changed no probe. What remains
  true is the mechanism: an ingress policy on a probe's **control target** can break the probe,
  where one on the probe pod itself cannot.
- **The `flaresolverr` exemption's rationale expires.** It is exempt from the labelling guard today
  (Ruling 4, Task 1) because its own bespoke policy is *tighter* than the baseline. Under
  `podSelector: {}` the baseline selects it anyway regardless of the exemption, producing exactly
  the widening — traefik, `prometheus`, and every node CIDR admitted to a pod that today accepts only
  `prowlarr` — the exemption was written to avoid.

### Rollback is a variable, never a deletion

`kubectl delete` is denied and `kubectl apply` does not prune, so **removing the template
leaves the live policy enforcing** (the repo's own orphaned-objects finding). The policy is
therefore *always* rendered, and `netpol_baseline_enforced: false` renders a permissive
body. Rollback is a one-line var flip plus a deploy.

The two shapes are exact inverses and are the easiest thing here to get backwards:

- `ingress: []` — empty list — **denies everything**
- `ingress: [{}]` — one empty rule — **allows everything**

### `from` peers: OR vs AND

Separate list items combine with OR; sibling keys within one item combine with AND. This is live in
slice 1 (traefik → `grafana`:3000, `prometheus`:9090) and getting it wrong yields a policy that
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
| **2** ✅ | Media stack + bridges: sonarr, radarr, prowlarr, bazarr, tdarr, qbittorrent, configarr, janitorr, monitor-bridge, autofix-bridge | Densest genuine app-to-app mesh; several callers are DB-configured and unprobeable. **Deployed 2026-08-17.** jellyfin moved to slice 4 — see below |
| **3** ✅ | `observability` namespace | Four hostPort ingress paths, cross-namespace inbound from three homelab workloads, thick intra-namespace mesh. **Deployed 2026-08-19.** |
| **4** ✅ | Infra tier: traefik, authelia, `crowdsec`, pihole, mosquitto, nut, jellyfin | Highest consequence; do it once the pattern is proven. **Deployed 2026-08-20.** registry/headlamp/n8n were NOT labelled — each already carries a bespoke policy *tighter* than the baseline, so labelling would widen it |
| **5** | Switch `netpol_baseline_scope` to `namespace` | Makes a workload fenced-by-default instead of opt-in. Gated on zero unlabelled pods |

**Services born fenced.** A leaf app added after slice 1 shipped belongs to no slice, but has
slice 1's shape: Traefik is its only caller and it dials nothing. Those carry the label from
their first deploy rather than waiting for slice 5 to sweep them up, and
`ansible/tests/test_netpol_baseline_labels.py` lists them in `BORN_FENCED_ROLES` so the set
stays as explicit as the slices. So far: **artifacts** (2026-08-19), the read-only browser over
each host's `~/.claude/artifacts`; **docs** (2026-08-24), nginx over a hostPath of built MkDocs
output, whose site is generated on the host by the `docs-refresh` cron so the pod never fetches,
clones or resolves anything; **texbrain** (2026-08-26), nginx over a static LaTeX editor baked
into the image. texbrain is worth a second look because it appears to contradict the shape: the
app reaches jsDelivr for on-demand TeX packages and a CORS proxy for git. Those are the
browser's fetches, made from the reader's machine, not the pod's — the container serves files
and opens no connection of its own.

**Why observability moved from first to third.** It is small in pod count but dense in
exactly the paths that are hardest — `loki:3100`, `tempo:3200`, `prometheus:9090` and
`otel-collector:4317` all take traffic over hostPorts (that last one is how both hosts'
Claude Code exports OTLP), plus inbound from monitor-bridge, homelab-mcp and Traefik. It is
also the instrument you would use to notice that a later slice broke something, so fencing
it first means debugging slices 2–4 with the monitoring possibly impaired.

### Slice 3 specifics

- **A second baseline object, not a wider selector.** A NetworkPolicy only ever selects pods in its
  own `metadata.namespace`, so `observability` needed its own ingress baseline
  (`netpol-baseline-observability`, plus two per-workload policies) rather than more labels on the
  existing `homelab` one. It is one role's worth of templates (`netpol-baseline`) but fences six
  workloads once it enforces — `claude-otel` renders `prometheus`, `loki`, `tempo`, the otel-collector,
  `grafana` and kube-state-metrics as six separate pod-producing documents from one role. Counting
  roles, the fenced total becomes 17 once this slice enforces (6 slice-1 + 10 slice-2 +
  `claude-otel`); counting workloads it is considerably higher — the two numbers answer different
  questions and this slice is the first place they diverge by more than one.
- **There are two Loki instances, and a name grep cannot tell them apart.** `homelab-mcp` (in `homelab`) is
  the only in-cluster caller of *this* Loki, reaching it via `CLAUDE_LOKI_URL`. `terraria-stats`,
  `valheim-stats` and `monitor-bridge` all target a different Service, `loki-homelab` in `homelab`,
  via the plain `LOKI_URL`. Both are plausibly "the Loki" from a manifest name alone; telling them
  apart needed the **env value the manifest actually sets**, not the Service name either role's
  template happens to use.
- **Neither web route is unauthenticated, so the probe has no HTTP liveness leg.** `grafana` is
  `use_authelia: true`. The `prometheus` route is **ClientIP LAN-gated plus rate-limit, not
  Authelia** (`claude-otel/templates/prometheus-ingressroute.yaml.j2`) — worth stating precisely
  because an earlier draft of this section called it Authelia-gated, and the two middlewares fail
  identically to an unauthenticated-route probe (a 401 or a 302, see "The liveness target must be
  an unauthenticated route" above) for different reasons. With no unauthenticated target available,
  the slice-3 probe substitutes an **EndpointSlice readiness gate** — proving the four backends
  have ready endpoints — for the HTTP liveness assertion earlier slices use.
- **`observability` gets its own node-CIDR list**, `netpol_baseline_obs_node_cidrs`, rather than
  widening the shared `homelab` baseline's two addresses. The policy that reads that shared list
  (`networkpolicy.yaml.j2`) lives in `homelab`, and a NetworkPolicy only ever selects pods in its own
  namespace — so widening the list would not have reached `observability` pods even if edited. It
  stays separate anyway for a real, independent reason: the 16 already-enforcing homelab roles read
  that list today, and editing it for observability's benefit would put all 16 at risk of a
  regression nobody would trace back to this namespace.
- **The intra-namespace mesh is a bare `podSelector: {}` peer.** The real justification is not "one
  role, six workloads" but that `observability` is **sole-tenant today** — `claude-otel` is the only
  role that renders into it. That is the invariant the design leans on, and it is exactly what a
  second role landing in the namespace later would silently invalidate: it would get free ingress to
  every already-fenced pod there with no policy change of its own.
- **`otelq` is a host process, not a cluster caller**, reading `loki:3100`, `prometheus:9090` and
  `tempo:3200` over loopback hostPorts, admitted by the node-CIDR `ipBlock` peer (which carries no
  `ports` restriction — trimming that CIDR list to "nameable" entries would break otelq silently).
  Both `cni0` entries (`10.42.0.1/32`, `10.42.1.1/32`) are load-bearing, not just daniel-box's: a hostPort binds
  only on the node the pod actually runs on, and `prometheus`/`loki`/`tempo` are unpinned single-replica
  Deployments, so either node can be the one otelq's loopback path resolves to. Only
  `10.42.1.0/32` (daniel-server's `flannel.1`) has no caller nameable today; it stays as deliberate
  over-inclusion, at the same trust level the baseline already grants node bridges.

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

### 1. `registry`'s `10.42.1.0/32` is daniel-server's `flannel.1` — a real address, do not remove it

`kubectl -n homelab get pod -l app=registry -o jsonpath='{.items[0].spec.nodeName}'` → **daniel-box**,
whose podCIDR is `10.42.0.0/24`. Registry serves containerd over a hostPort on the node it runs on,
so `10.42.0.1/32` is the entry doing the work for daniel-box's own pulls.

`10.42.1.0/32` is not a `cni0` address (both nodes' `cni0` is `.1`), but it is **not** the inert
network address of daniel-server's CIDR either — measured, `10.42.1.0` is daniel-server's
**`flannel.1`**, the VXLAN interface, and `10.42.0.0` is daniel-box's. It is an interface address
that traffic really does source from.

**Slice 4 must therefore not delete it, and must not "replace" it with `10.42.1.1/32`.** An earlier
version of this section gave exactly that instruction, on the belief that the entry matched nothing.
That belief was wrong. Whether daniel-server's containerd actually pulls through `flannel.1` rather
than `cni0` is unproven in either direction — the point is that the address is real, so removing the
entry needs evidence, not an assumption. Registry is not pinned to a node, so a reschedule onto
daniel-server is the case that entry may well be covering.

### 2. Pod → MetalLB VIP traffic is SNAT'd to the node bridge address, despite ETP=Local

Measured against the live mosquitto:

- zigbee2mqtt's pod IP: `10.42.0.242` (on daniel-box)
- every line in mosquitto's log: `New connection from 10.42.0.1:<port> on port 1883`

`10.42.0.1` is daniel-box's `cni0`. **ETP=Local preserves the source IP for traffic arriving from
outside the cluster, not for a pod dialling its own cluster's LoadBalancer VIP** — that path
hairpins through the node and is masqueraded. No pod IP appears in the log at all.

Consequence for slice 4: **a `podSelector` cannot express "mosquitto accepts zigbee2mqtt."** The
rule must use the node `ipBlock`s, which also means it cannot distinguish zigbee2mqtt from any
other pod on the same node — so fencing mosquitto by source is not achievable while the client
dials the VIP. Either accept that, or move the client to the ClusterIP so pod identity survives.

The baseline's existing `10.42.0.1/32` and `10.42.1.1/32` entries already admit this path, so any
workload fenced by the baseline remains reachable over a VIP hairpin today.

## Answers from slice 2

Deployed 2026-08-17. Ten workloads labelled, four per-workload allow policies live.

### 3. Host → ClusterIP is admitted by the baseline's `cni0` `/32`s — only on the caller's own node

Slice 2 was built as the experiment for this rather than pre-allowing both address forms.
`roles/k8s/sonarr/tasks/verify.yml` calls sonarr's ClusterIP from the Ansible controller on
daniel-box on every deploy; with sonarr fenced and **no node-IP ipBlock in its policy**, that verify
passed and returned real data (14 series, 20.8 GiB measured through the mount). So a host caller
does arrive as the node's `cni0` address, and the baseline's existing `10.42.0.1/32` and
`10.42.1.1/32` entries cover it — **when the pod is on the same node as the caller, and only then.**

Cross-node, host → fenced pod is **blocked today**. Not unproven: measured, 2026-08-17.

| caller host | target | target's node | fenced | result |
|---|---|---|---|---|
| daniel-box | prowlarr `:9696` | daniel-server | yes (slice 2) | **000 — blocked** |
| daniel-box | littlelink `:3000` | daniel-server | yes (slice 1) | **000 — blocked** |
| daniel-box | healthchecks `:8000` | daniel-server | yes (slice 1) | **000 — blocked** |
| daniel-box | ical-proxy `:5000` | daniel-server | yes (slice 1) | **000 — blocked** |
| daniel-box | freshrss `:80` | daniel-server | **no** (control) | 200 |
| daniel-box | sonarr `:8989` | daniel-box | yes (slice 2) | 200 |
| daniel-server | sonarr `:8989` | daniel-box | yes (slice 2) | **000 — blocked** |
| daniel-server | jellyfin `:8096` | daniel-box | **no** (control) | 302 |
| daniel-server | littlelink `:3000` | daniel-server | yes (slice 1) | 200 |

The two unfenced controls reach across nodes fine, so the blocks are the policy, not routing. The
result shows the cross-node source is neither of the two `cni0` `/32`s.

**Strong evidence that it is `flannel.1`, short of a packet capture.** `registry` is a fenced pod
running on daniel-box, and its policy admits exactly two host-shaped sources on `:5000` —
`10.42.0.1/32` (daniel-box `cni0`) and `10.42.1.0/32` (daniel-server `flannel.1`). Every node's
containerd is configured to pull through that ClusterIP (see the caller below), and `ical-proxy` —
scheduled on daniel-server, `imagePullPolicy: Always` — started at 2026-08-16T23:36:13Z with a
resolved digest, *after* registry's policy was live. The only entry that could have admitted that
cross-node pull is `10.42.1.0/32`. Not proof (no capture, and no cold pull was forced), but it is
the answer slice 3 needs, and it is why the slice-4 instruction to remove that entry was withdrawn.

**Scope, stated honestly: this is a pre-existing hole from slice 1, not something slice 2
introduced.** littlelink, healthchecks and ical-proxy are slice-1 workloads and were already in it
the moment slice 1 was enforced. Slice 2 adds exactly one daniel-server workload to the set,
prowlarr.

**And no caller is broken today**, but the census that establishes this took three passes to get
right, so treat the list as the deliverable and the method as insufficient.

Host → ClusterIP callers targeting a **fenced** pod:

1. `roles/k8s/sonarr/tasks/verify.yml` → sonarr, from daniel-box.
2. `roles/setup/fake_remux/tasks/main.yml` → sonarr, from daniel-box.
   Both are same-node and stay that way: sonarr is pinned to daniel-box by the `media-data` PV's
   *required* `nodeAffinity` (`roles/k8s/media-volume/templates/pv.yaml.j2`).
3. **containerd on daniel-server → `registry`, cross-node, and working.**
   `roles/setup/k3s/templates/registries.yaml.j2` renders
   `endpoint: http://{{ k8s_registry_cluster_ip }}:{{ k8s_registry_port }}` on every host that is
   not the registry node, installed by `roles/setup/k3s/tasks/agent.yml`. registry is fenced and
   runs on daniel-box. This is the cross-node host path succeeding in production, and it is the
   evidence above.

A fourth targets `prometheus` in `observability`, which no policy selects until slice 3 — the warning
below.

**The census method that missed one, and why.** Two passes used
`grep -rn clusterIP --include='*.j2' --include='*.yml' ansible/roles/`, on the reasoning that a host
caller cannot use cluster DNS and must resolve a ClusterIP first. That is true but insufficient: the
registry caller references the **variable** `k8s_registry_cluster_ip`, whose name does not contain
the string `clusterIP`, so the grep cannot see it. The earlier draft compounded this by dismissing
registry's one hit as "a static Service field, not a caller" — which closed the door on following
the variable to its host consumer.

For slices 3–5, grep for the variables too (`_cluster_ip`, `_clusterip`), and check
`roles/setup/**` as well as `roles/k8s/**` — a host caller is more likely to live in host setup than
in a workload role.

**Slice 3 found two more gaps in the same census, worth folding into the method for slices 4–5.**
First: a role is not the unit of fencing. Nine roles render more than one pod-producing document, so
a census scoped to `containers_list` entries undercounts every one of them — `claude-otel` (6, this
slice), `karakeep` (4), `scrutiny` (3), `prowlarr` (2, already handled via the flaresolverr
exemption, Ruling 4), `n8n` (2), `loki-homelab` (2), `freshrss` (2), `crowdsec` (2),
`cloudflare-ddns` (2). None of the unhandled ones are in scope for slices 1–3. Whichever slice
fences one of these roles next must check for a second workload AND for an existing bespoke policy
covering it, the way Task 1 did for flaresolverr — the role-granular habit silently leaves the
second workload unfenced. Second: two services can share a **name** across namespaces — see "Two Loki
instances" in "Slice 3 specifics" above. Resolve the env value the manifest actually sets, not the
Service name a role's own template happens to use.

**Warning for slice 3, where that stops being true — corrected below.**
`roles/k8s/claude-otel/templates/telemetry-health.sh.j2` resolves `prometheus`'s ClusterIP at runtime
(`kubectl get svc prometheus -o jsonpath='{.spec.clusterIP}'`, then curls it). An earlier draft of
this section claimed the cron installing it "runs from a host cron on both nodes." **That is false,
verified:** `claude-otel` appears in `containers_list` only in `host_vars/daniel-box.yml`;
`daniel-server.yml` has no entry, and the cron task in `roles/k8s/claude-otel/tasks/main.yml` has no
`delegate_to`. The cron installs and fires from daniel-box alone.

That does not remove the cross-node exposure, it relocates it. `prometheus` has **no node pin** —
`roles/k8s/claude-otel/templates/prometheus.yaml.j2` sets no `nodeSelector`, `nodeAffinity` or
`nodeName` — so the moment `prometheus` lands on daniel-server, the daniel-box cron's call becomes
cross-node, silently and without a deploy to blame it on. By slice 2's own evidence ("Answers from
slice 2" above, the `registry`/`flannel.1` finding), a cross-node host→pod packet arrives as the
**sending** node's `flannel.1` address — so that caller needs daniel-box's own flannel.1
(`10.42.0.0/32`), not daniel-server's. Slice 3 must either admit that address or leave `prometheus`
reachable from both hosts before it labels the observability namespace. See "Slice 3 specifics"
above for what it settled and verified per-node with `ip -4 -o addr show flannel.1`.

### Three callers the census missed, and what they have in common

All three were found during implementation, not by the sweep, and all three would have broken
something:

1. **`prowlarr` → sonarr and radarr.** Application Sync POSTs indexer definitions *into* both *arrs.
   The census recorded only the query direction. Fenced, both would silently keep stale indexers —
   and monitor-bridge would not notice, because its "Prowlarr Indexers" monitor reads prowlarr's
   *own* status endpoint.
2. **A probe's CONTROL leg.** `prowlarr/templates/netpol-probe-job.yaml.j2` retries
   `nc -z prowlarr` for up to 150s from a pod labelled `app: flaresolverr-netpol-probe`. The census
   looked for assertions, not controls.
3. **A pod created by `kubectl run` inside a verify task.** `qbittorrent/tasks/verify.yml` creates
   an unlabelled probe pod whose stated purpose is proving qbittorrent is reachable *from the pod
   CIDR* despite the Mullvad kill-switch — which the policy would have silently inverted.

**The lesson for slices 3-5:** a caller census built from manifests alone is not sufficient. It must
also be asked for probe and verify **control** legs, and for `kubectl run` / `kubectl exec` inside
`tasks/*.yml`. Precedent for admitting deploy-time test pods already existed in
`registry/templates/networkpolicy.yaml.j2`, which admits `registry-selftest-push/pull`.

### What was verified live after the deploy

- Both probes green. Slice 2's asserts sonarr and qbittorrent are unreachable from a pod on nobody's
  allow list, with a control and an unfenced negative control.
- `OK sonarr/8989`, `OK radarr/7878`, `OK jellyfin/8096` from inside the janitorr pod — the
  positive path, from a fenced caller to fenced callees.
- `OK arr_queue - queue clean (Sonarr, Radarr)` from monitor-bridge at 03:26, after both were
  fenced — the monitor-bridge allow list proven by the real monitor, not a probe.
- The flaresolverr probe's control leg printed `control ok: prowlarr:9696 reachable from this pod`,
  and qbittorrent's reach-probe reported reachability from the pod network with egress still forced
  through Mullvad — the two missed callers, confirmed fixed.

### One rough edge, not caused by the policies

`janitorr/tasks/verify.yml` failed once on its cleanup step: it exec'd a pod name captured before
the label change rolled the Deployment, and by then that pod was gone
(`cannot exec into a container in a completed pod`). Its substantive checks had all passed. A
re-run was clean. This is a latent race in that verify — it will recur on any change that rolls
janitorr — and is worth fixing independently of this work.

## Answers from slice 3

Deployed 2026-08-19 in two stages, as designed: `claude-otel` first to add the labels while
`netpol_baseline_obs_enforced` stayed `false`, then `netpol-baseline` to arm the fence.

- **The two-stage split earned its keep, for a reason the plan did not anticipate.** Stage A rolled
  all six workloads and the scrape-target count moved from 25 to 29. Every one of the extra four was
  a dead pod's series still inside Prometheus's lookback window, not a new target — but that is only
  legible because stage A changed the labels and nothing else. Had the fence gone live in the same
  deploy, the same reading would have had two candidate causes and no way to separate them.
- **Stage B rolled nothing, as predicted.** All seven pods kept the start times from the stage-A
  roll. A NetworkPolicy change restarts nothing, so arming the fence is observable only in the
  probe and the callers.
- **Stage A deployed stale templates, and that is the session's most reusable finding.** The
  worktree was 48 commits behind master, and `scripts/deploy.sh` renders from whatever tree it
  runs in, so stage A reverted `claude-otel` to its state at the branch point. Live for roughly
  nine minutes (22:14–22:23): the longhorn scrape lost the endpoint-based service discovery that
  scrapes both `longhorn-manager` pods, falling back to a single Service target, and
  `telemetry-health.sh` was reverted alongside it. Rebasing onto master and redeploying restored
  both. Nothing about this is specific to NetworkPolicies — **any** worktree-isolated session that
  is behind master silently reverts live config for the roles it deploys, and every repo-side
  check reads green because the tree it renders from is internally consistent. It surfaced here
  only because a scrape-target count moved and got chased rather than waved through. The durable
  fix is a check in `deploy.sh` — refuse, or at least warn, when `HEAD` is behind
  `origin/master` — not a paragraph in a design doc.
- **The probe is the proof, not the witnesses.** `netpol-baseline-probe-slice3` reported its control
  target reachable (`traefik:80`) and both fenced services unreachable from an unlisted caller. The
  scrape-target count returning to 25 shows the admitted callers still work; only the inverted
  assertions show the fence denies anything.
- **The intra-namespace `podSelector: {}` peer is proven, and it proves additivity too.**
  Prometheus scrapes kube-state-metrics, `loki:3100` and `tempo:3200` — all three fenced, all
  three reading `up == 1` after the fence. The `loki` case is the interesting one: `loki-callers`
  admits only `homelab-mcp` on 3100, so `prometheus` reaches `loki` solely because the baseline's
  `podSelector: {}` rule unions with it. Grafana's own outbound path to those three datasources
  rides the same rule but was not separately exercised — no dashboard was loaded in the
  verification window, and an absence of errors in `grafana`'s log is not evidence that it queried
  anything.
- **A 302 from the `grafana` route proves nothing**, for the reason already recorded elsewhere in this
  doc: Authelia's redirect fires in the middleware, before Traefik proxies to the backend. The
  Traefik cross-namespace peer was proven instead through the `prometheus` route, which carries no
  Authelia — it returned HTTP 200 with a real result body.
- **Only one of the four node CIDRs was exercised.** Both `loki` and `prometheus` were scheduled on
  daniel-box, and both host callers run there too: the `telemetry-health.sh` heartbeat (exit 0
  through the fence) and the loopback hostPort path to `loki` (HTTP 200). Both are therefore
  same-node, which the earlier slices measured as arriving from `cni0` — consistent with the
  inference this slice made, and the same-node hostPort DNAT source is now exercised rather than
  merely inferred. The other three entries — daniel-server's `cni0` and both `flannel.1`
  addresses — are still unexercised. They cover the case where a pod moves nodes, which is
  precisely why the list was not trimmed to what a single day's placement happens to need.

## Answers from slice 4

Deployed 2026-08-20. Policies applied first, then labels one role at a time, traefik last. All four
slices' probes now pass together for the first time; 29 pods carry `netpol-baseline: enforced`.

**Two defects survived every review and were found only by deploying.** Both are the same mistake
in different clothing: the caller census asked *"which pods dial this Service, on which Service
port."* Both halves of that question are wrong.

- **A NetworkPolicy matches the POD's port, not the Service's.** kube-proxy has already DNAT'd
  Service port → targetPort by the time the policy is evaluated. traefik's Service maps 80→`http`
  and 443→`https`, whose containerPorts are **8000 and 8443**, so a policy naming 80/443 admitted
  two ports nothing listens on and denied every in-cluster caller of the real ones. The symptom was
  diagnostic and misleading: the probes failed on `cannot reach traefik:80` while the same route
  answered **200 from the LAN**, because external traffic arrives over the VIP path rather than the
  ClusterIP. Every review verified the ports against the Service — the wrong reference — because
  that is what they were asked to check. **Check `targetPort` for every service a policy fences.**
  In this slice only traefik diverged; the other six map 1:1.
- **A sidecar has no network identity of its own — it carries its POD's labels.** authelia runs a
  `crowdsec-agent` sidecar shipping auth logs to the LAPI, so that traffic arrives as
  `app: authelia`, which the `crowdsec` policy did not admit. kube-router REJECTs rather than drops,
  so the sidecar crash-looped on "connection refused," which made the whole pod unready, which
  pulled authelia out of its Service endpoints, which **broke SSO on every protected route for
  ~17 minutes**, while the authelia container itself stayed healthy throughout. Find them with
  `grep -rl crowdsec-agent ansible/roles/k8s --include='*.j2'` — here: traefik, authelia, and the
  node-agent DaemonSet.

**Two callers the review caught before deploy, both confirmed live afterwards.**
- Home Assistant reaches nut cross-node over the ClusterIP; `sensor.ups_power` kept updating after
  nut was fenced. Same-node *and* cross-node pod→ClusterIP→pod both **preserve the source pod IP** —
  it is not masqueraded to cni0, which several earlier conclusions in this project assumed.
- zigbee2mqtt logged `Connected to MQTT server` after mosquitto's roll. This is the live proof the
  mosquitto podSelectors are load-bearing: an earlier reading of the connection log concluded they
  had "never matched a real connection," because z2m and HA hold **persistent** MQTT sessions that
  never reappear in a log window, and the connections that *were* visible were kubelet probes at
  the readiness/liveness periods.

**The rollback lever is wider than one slice.** `netpol_baseline_enforced` gates the baseline too,
so disarming it to unstick a slice-4 workload also unfences the 16 slice-1/2 workloads. Reverting a
single workload's label is the narrower undo.

## Slice 4.5 — the workloads no slice owns

> **Superseded in two details by "Answers from slice 4.5" below**, which is the record of what
> shipped: the real count was 27, and headlamp / n8n / registry are out of scope because they are
> already born fenced.

Slice 4 does **not** unblock slice 5. `homelab` holds 56 controllers; slices 1-4 and the
`flaresolverr` exemption cover 27 of them, leaving **28 that belong to no slice** — and slice 5's
gate fails on any unlabelled pod. They fall in four groups:

| Group | Count | Members |
|---|---|---|
| Standalone apps | 13 | home-assistant, homelab-mcp, homepage, freshrss, karakeep, `livesync`, `loki`-homelab, peanut, uptime-kuma, wg-easy, zigbee2mqtt, cloudflare-ddns-direct, cloudflare-ddns-proxied |
| Sub-workloads of a parent role | 7 | karakeep-chrome, karakeep-meilisearch, karakeep-time-tagger, `scrutiny`-influxdb, `scrutiny`-web, freshrss-feed-cache, n8n-runners |
| DaemonSets — fence or exempt | 4 | node-exporter, promtail, `scrutiny`-collector, `crowdsec`-node-agent *(already fenced in slice 4)* |
| Deferred since slice 1 | 4 | terraria, valheim, terraria-stats, valheim-stats — LAN game clients reach them over their own MetalLB VIPs, so they need per-workload peers no slice has designed |

Slice 4.5 must apply slice 4's two lessons from the start: check `targetPort`, not the Service
port, and enumerate sidecars, which carry their host pod's identity.

## Answers from slice 4.5

Deployed and enforcing 2026-08-20. Two corrections to the section above, and four things the
rollout settled that the plan did not predict.

**The count was 27, not 28, and three named workloads are out of scope.** The cluster held 31
unlabelled controllers, of which `headlamp`, `n8n`, `registry` and `flaresolverr` already carry
their own policies. Labelling them would ADD the baseline's traefik + `prometheus` + node-CIDR
allow-list on top of a tighter bespoke one, widening them. `n8n` is now recorded in the guard's
`BESPOKE_POLICY_WORKLOADS` for that reason — slice 4.5 labels `n8n-runners`, which drags the role
into the fenced set and would otherwise demand a label on `n8n` itself.

**The probes' unfenced control had to be replaced before anything else.** Slices 1 and 2 proved
their probe pod had a network by dialing `homepage:3000`, which worked only because homepage
happened to be unfenced. Slice 4.5 fences homepage and slice 5 leaves no unfenced pod at all.
`networkpolicy-netpol-sentinel.yaml.j2` now admits pods labelled `netpol-probe: control` to
`speedtest:80`, and that is strictly stronger than the leg it replaced: reaching an unfenced pod
proved only that the probe had a network, where reaching a FENCED pod that admits us proves the
admit path itself works. Not `littlelink` — that is slice 1's DENIED target, and a target cannot
be both admitted and denied.

**A caller census that excludes a workload's own role misses intra-role callers.** The census
searched each name across the tree while excluding its own role directory, to filter out
self-references — which also filtered out every caller living in the same role as its callee.
`karakeep-time-tagger`'s `wait-for-karakeep` init container dials `karakeep:3000`, and denying it
crashes nothing: the new pod wedges in `Init` forever while the OLD pod keeps serving, so the
Deployment reads 1/1 and the workload looks healthy while the rollout silently never completes.
When fencing a role that renders several workloads, grep the role's own templates for
`create_connection\|nc -z\|nc -w` rather than trusting a cross-role census. The other two
intra-role dialers are fine: `n8n-runners → n8n:5679` is admitted by the pre-existing n8n-broker
policy, and karakeep's calls to chrome and meilisearch are covered by this slice.

**A probe leg that depends on the probe pod's OWN labels needs a retry.** The sentinel leg failed
live with the policy, the pod's labels and the target all verified correct, and kept failing 15
minutes after the policy was applied — so not policy reconciliation lag. Every other leg in every
probe either dials a rule with no `from:` or asserts a denial, and neither needs kube-router to
resolve the SOURCE pod. The sentinel is the only leg whose match depends on this pod's own labels
being known, and a Job pod is seconds old when it dials. It now retries for up to 60s, which made
it pass with nothing else changed.

**Deploy order: workload roles first, netpol-baseline last — but a partial run is the hazard.**
A per-workload policy alone does not admit traefik, so applying policies before labels fences every
UI off from the front door. The natural `containers_list` order already puts netpol-baseline last,
which is correct. What is NOT safe is treating one combined run as atomic: this deploy aborted at a
300s rollout wait, 11 roles never received their labels, and netpol-baseline then applied ALL their
policies — producing exactly the policies-first state the ordering exists to avoid. The Home
Assistant, Uptime Kuma, Scrutiny and Loki routes returned 5xx for roughly 17 minutes until the
remaining labels landed. Game and VPN ports were unaffected, because those policies leave their
port open regardless of the label. **Deploy the workload roles to completion and verify every label
landed BEFORE running netpol-baseline.**

## What slice 5 inherits

- The born-fenced widening problem: `podSelector: {}` adds the baseline's allow-list on top of
  headlamp's, n8n's and registry's own policies. Slice 5 must decide whether to exempt them.
- Transient Job pods — buildkit image builds, the probes themselves, `pi-peer-backup` — are
  selected by a namespace-scoped selector. Being selected costs them nothing: every one of them
  is outbound-only, and an Ingress policy governs what reaches a pod, not what it reaches (return
  traffic rides conntrack). The buildkit Jobs are the one worth checking rather than assuming, and
  they are fine too: `buildctl-daemonless.sh` starts a buildkitd inside the pod, so the build's
  only client is loopback.
- Traffic a VPN client sends onward to a cluster service is routed through the wg-easy pod and
  arrives as `app: wg-easy`. Nothing depends on it today, because VPN clients reach services by
  hostname through Traefik, but a service dialled by ClusterIP over the VPN would need that peer.

## What slice 5 decided

**Deployed and enforcing since 2026-08-20.** Both baselines select
`matchExpressions: [{key: netpol-baseline-exempt, operator: DoesNotExist}]` live: 81 pods fenced in
`homelab` and 4 exempt. All five netpol-baseline probes passed on the first run, and so did the
prowlarr, headlamp and registry probes during stage A — **no probe needed changing**, which is the
measured form of the correction above. 25 Prometheus targets up, monitor-bridge all-OK including
`cluster_targets - all 24 targets up`. Spot checks across the fencing shapes: home-assistant 200,
uptime-kuma / `scrutiny` / karakeep / n8n / headlamp 302, registry 200 over its node hostPort (it has
no public route, so the edge 404s by design) with both self-test pull Jobs complete.

The two-stage deploy ran clean and nothing 5xx'd. That is the part worth not over-reading: slice
4.5's outage came from an abort mid-run, which stage discipline reduces but does not eliminate.

The plan and its caller analysis are in `docs/archive/networkpolicy-slice5-plan.md`. Four decisions are
durable enough to record here rather than only there.

**The selector is an opt-out, not `podSelector: {}`.** A bare `{}` would select the four workloads
that own a policy *tighter* than the baseline — flaresolverr, headlamp, n8n, registry — and, because
policies are additive, add traefik, `prometheus` and both node CIDRs on top of each. That is the exact
widening the flaresolverr guard exemption was written to avoid, and the spec above predicts its
rationale expiring here. Instead the selector is `matchExpressions: [{key: netpol-baseline-exempt,
operator: DoesNotExist}]`, and those four workloads carry the label. `DoesNotExist` rather than
`NotIn: ["true"]`: a pod with no such key satisfies `DoesNotExist` unambiguously.

**The exemption goes on n8n and not on n8n-runners.** `n8n-broker` selects `app: n8n` only, so
`n8n-runners` is fenced by the baseline alone. The two pod templates sit side by side in one role.

**The gate reads the cluster, not the templates.** The spec's gate — enumerate pods lacking the
label, fail if non-empty — inverts under an opt-out, where unlabelled is the *fenced* case. It is
replaced by an exact set comparison, in both directions, against live pods: a name missing is a
workload about to be widened, and a name present but unexpected is a pod fenced by nothing.
Template-level is not enough, and slice 4.5 is why: `n8n-runners` rendered its label while the live
Deployment did not carry it, for ~16 hours, with `test_netpol_baseline_labels.py` green throughout.

**The deploy hazard inverts too.** Slice 4.5's partial run left policies applied with labels
missing, and every fenced UI returned 5xx — loud. Here a partial run leaves the four exempt
workloads *widened* instead, which nothing observes. So the exempt labels deploy first and are
verified live before the scope flips.

## Open items still outstanding

1. Whether a lint should require every `containers_list` entry to have a policy — the
   baseline makes a missing policy safe rather than broken, so this is optional, but it is
   the executable-check end of the repo's escalation ladder. Slice 1 shipped a narrower
   version of this: `ansible/tests/test_netpol_baseline_labels.py` pins the labelled set.
2. The probe's `activeDeadlineSeconds` (120s) is shorter than the Ansible wait (180s), so a
   deadline-exceeded run produces a misleading diagnosis in either branch of the fail message.
   Slice 1's probe completed well inside both, so the real timings are now known and this can
   be tuned rather than guessed.
