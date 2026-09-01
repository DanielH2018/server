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

What each deploy settled, slice by slice, is in
[networkpolicy-slice-answers.md](networkpolicy-slice-answers.md).

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

## Open items still outstanding

1. Whether a lint should require every `containers_list` entry to have a policy — the
   baseline makes a missing policy safe rather than broken, so this is optional, but it is
   the executable-check end of the repo's escalation ladder. Slice 1 shipped a narrower
   version of this: `ansible/tests/test_netpol_baseline_labels.py` pins the labelled set.
2. The probe's `activeDeadlineSeconds` (120s) is shorter than the Ansible wait (180s), so a
   deadline-exceeded run produces a misleading diagnosis in either branch of the fail message.
   Slice 1's probe completed well inside both, so the real timings are now known and this can
   be tuned rather than guessed.
