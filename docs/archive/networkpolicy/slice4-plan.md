# NetworkPolicy Slice 4 — the infra tier

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fence the infra tier — traefik, authelia, crowdsec, pihole, mosquitto, nut, jellyfin —
behind ingress NetworkPolicies, and bring headlamp, n8n and registry under the same label.

**Architecture:** Same shape as slices 1–3. Each workload gets `netpol-baseline: enforced` on its
pod template, which makes the existing `homelab` baseline policy select it; workloads needing peers
the baseline does not grant get their own per-workload policy alongside it, because policies are
additive. Everything is gated on `netpol_baseline_enforced`, already `true`, so **slice 4's fences
go live the moment the labels land** — unlike slice 3, there is no second flag to flip.

**Tech Stack:** Kubernetes NetworkPolicy (kube-router), Ansible, Jinja2, pytest

**Spec:** `docs/networkpolicy-default-deny.md` — read it, especially *Traefik's own ports stay
open* and *Slice 4 specifics*.

## Global Constraints

- **kube-router enforces Ingress only.** Egress policies select pods and block nothing. Never
  write an egress rule and never claim one protects anything.
- **NetworkPolicies are namespaced.** Every workload here is in `homelab`; prometheus is in
  `observability` and is therefore a **cross-namespace** peer needing `namespaceSelector` +
  `podSelector` as siblings in one `from` item.
- **Policies are additive.** The union of every policy selecting a pod is what it admits. A policy
  selecting a pod is precisely what makes that pod default-deny.
- `ingress: []` denies everything; `ingress: [{}]` allows everything. Exact inverses.
- Within one `from` list item, sibling keys AND. Separate list items OR.
- **Kubelet probe traffic bypasses NetworkPolicy here** (proven slice 1). Readiness/liveness
  probes are never a reason to add a peer.
- Rollback is a variable flip plus redeploy, never a `kubectl delete` — deletion is denied
  cluster-side and `kubectl apply` does not prune.
- Node addresses: `cni0` = 10.42.0.1 (box) / 10.42.1.1 (server); `flannel.1` = 10.42.0.0 (box) /
  10.42.1.0 (server). Same-node host→pod arrives as `cni0`; cross-node as the **sending** node's
  `flannel.1`.

---

## What the census found

Run 2026-08-19 against the live cluster, not from the spec's prose. Four things differ from what
the spec assumes.

### 1. Slice 4 is smaller than its name list — three workloads are already fenced

`headlamp`, `n8n` and `registry` already carry bespoke policies (`kubectl -n homelab get
networkpolicy`), so they are **already default-deny**. They are missing only the label. The
genuinely new fences are **seven**: traefik, authelia, crowdsec, pihole, mosquitto, nut, jellyfin.

Registry's policy already admits both node paths, which slice 1 recorded: containerd on daniel-box
reaches it through a loopback hostPort (same-node, `cni0`), and containerd on daniel-server reaches
the pinned ClusterIP over flannel (cross-node, `10.42.1.0/32`). Do not touch it beyond the label.

### 2. CoreDNS is a *pod* caller of pihole — the spec says otherwise, and is wrong

The spec's *Slice 4 specifics* says pihole's ":53 callers are hosts, not pods". Measured: the
CoreDNS Corefile forwards to `10.43.252.187`, which is `pihole-dns`'s ClusterIP:

```
forward . 10.43.252.187 1.1.1.1 1.0.0.1 {
```

So **every cluster DNS query that Pi-hole answers arrives from a CoreDNS pod in `kube-system`**.
Fencing pihole while admitting only node CIDRs and Traefik would break DNS for the whole cluster —
the single largest blast radius in this slice. CoreDNS must be an explicit cross-namespace peer.

This is the same class of error slice 3 hit with the two Lokis: the spec's caller list was written
from reasoning, not from resolving the live config.

### 3. `pihole` is an HA pair, and the Service selectors differ from the pod selector

Two controllers, one role: `pihole` (`app=pihole, instance=pihole`) and `pihole-2`
(`app=pihole, instance=pihole-2`). The `pihole` Service selects `app=pihole,instance=pihole` —
only the first — while the `pihole-dns` LoadBalancer selects `app=pihole`, both.

A policy written on `podSelector: {app: pihole}` therefore covers **both** instances, which is what
is wanted. Writing it on the two-label selector would fence only one and leave the other silently
open. Use the single label deliberately, and say so in the template.

### 4. What actually depends on Traefik's `:80` — corrected after review

An earlier draft of this census claimed uptime-kuma's monitors justify leaving `:80` open. That is
false, and the correction matters because this paragraph is the rationale a future auditor reads
before narrowing the front door. Measured against
`uptime-kuma/templates/static-monitors.yaml.j2`: **22 monitors use `https://`**, so they traverse
`:443`, and 3 more dial `daniel-pi` directly and bypass Traefik entirely. **Zero dial
`traefik:80`.**

Two things genuinely depend on `:80`:

- **The HTTP→HTTPS redirect entrypoint** (`traefik/templates/static-config.yaml.j2:36-42`,
  `redirections: entryPoint: to: ":443"`). Every client arriving on plain HTTP hits this before
  being redirected. This is the primary reason and was unstated until review.
- **The four netpol probe control legs** — exactly four files grep `nc -w 5 -z traefik 80`, each
  dialling it to prove the probe pod has a network at all before asserting anything.

uptime-kuma still matters to `:443`, and narrowing *that* would take the monitoring plane red in
one move. It is simply not evidence about `:80`.

### Caller matrix

| Target | Port | In-cluster callers | Node / external callers |
|---|---|---|---|
| `traefik` | 80, 443 | uptime-kuma (~20 monitors), 4 probe-job control legs | **all LAN + WAN** — stays open |
| `traefik-metrics` | 8080 | prometheus (**`observability`**) | — |
| `authelia` | 9091 | traefik only (forwardAuth middleware) | — |
| `crowdsec` | 8080 (LAPI), 7422 (appsec) | traefik bouncer, crowdsec-node-agent DaemonSet | — |
| `crowdsec` | 6060 | prometheus (**`observability`**) | — |
| `crowdsec-dashboard` | 3000 | traefik | — |
| `pihole` | 80 (UI) | traefik, homepage widget | — |
| `pihole-dns` | 53 | **CoreDNS (`kube-system`)** | LAN hosts — stays open |
| `mosquitto` | 1883 | zigbee2mqtt, home-assistant | via VIP `10.0.0.242`, **observe the source** |
| `nut` | 3493 | peanut | daniel-server's shutdown chain |
| `jellyfin` | 8096 | traefik, homepage, janitorr | LAN clients via VIP `10.0.0.241` |
| `headlamp` / `n8n` / `registry` | — | already fenced by bespoke policies | label only |

### What slice 4 does *not* do

`homelab` holds 56 controllers; 16 are fenced today. Slice 4's ten plus the `flaresolverr`
exemption still leaves **28 workloads belonging to no slice**, and slice 5's gate fails on any
unlabelled pod. Slice 4 therefore does **not** unblock slice 5. Task 9 records the remainder as
slice 4.5 rather than letting the slice plan keep reading as complete coverage.

---

## File structure

| File | Responsibility |
|---|---|
| `roles/k8s/netpol-baseline/templates/networkpolicy-traefik.yaml.j2` | traefik: open web ports, fenced metrics port |
| `roles/k8s/netpol-baseline/templates/networkpolicy-authelia.yaml.j2` | authelia: traefik only |
| `roles/k8s/netpol-baseline/templates/networkpolicy-crowdsec.yaml.j2` | crowdsec: traefik, node agents, prometheus |
| `roles/k8s/netpol-baseline/templates/networkpolicy-pihole.yaml.j2` | pihole: traefik on :80, CoreDNS + LAN on :53 |
| `roles/k8s/netpol-baseline/templates/networkpolicy-mosquitto.yaml.j2` | mosquitto: observed peers on :1883 |
| `roles/k8s/netpol-baseline/templates/networkpolicy-nut.yaml.j2` | nut: peanut + daniel-server node |
| `roles/k8s/netpol-baseline/templates/networkpolicy-jellyfin.yaml.j2` | jellyfin: traefik + LAN VIP |
| `roles/k8s/netpol-baseline/templates/netpol-probe-slice4-job.yaml.j2` | the slice-4 probe |
| `roles/k8s/{traefik,authelia,crowdsec,pihole,mosquitto,nut,jellyfin,headlamp,n8n,registry}/templates/*.yaml.j2` | add the pod-template label |
| `ansible/tests/test_netpol_baseline_labels.py` | extend the guard to slice 4 |
| `docs/networkpolicy-default-deny.md` | slice 4 answers + slice 4.5 definition |

**Policies live in `netpol-baseline`, not in each workload's role.** Slice 3 established this
(Ruling 2): a policy in the workload's own role would apply the instant that role deploys, outside
the enforcement flag, so the flag would stop being a true rollback.

---

## Task 1: Extend the label guard, and label the three already-fenced workloads

**Files:**
- Modify: `ansible/tests/test_netpol_baseline_labels.py`
- Modify: `ansible/roles/k8s/headlamp/templates/deployment.yaml.j2`
- Modify: `ansible/roles/k8s/n8n/templates/deployment.yaml.j2`
- Modify: `ansible/roles/k8s/registry/templates/deployment.yaml.j2`

**Interfaces:**
- Produces: `SLICE_4_WORKLOADS`, `SLICE_4_ROLES` — consumed by Tasks 2–8's tests.

Headlamp, n8n and registry are already default-deny via bespoke policies. Labelling them adds the
baseline's peers *on top* (policies are additive), which is a **widening** — exactly the trap
Ruling 4 refused for flaresolverr in slice 3. Before labelling each one, diff what its bespoke
policy admits against what the baseline admits. If the baseline is wider, do **not** label it; add
it to a `BESPOKE_POLICY_WORKLOADS`-style exemption instead and say why in the test.

- [ ] **Step 1: Read the three bespoke policies and record what each admits**

```bash
kubectl -n homelab get networkpolicy headlamp registry n8n-broker -o yaml | grep -A20 'ingress:'
```

Write the three peer sets into the ledger before changing anything. This step produces a decision,
not code.

- [ ] **Step 2: Add the slice-4 constants to the guard**

```python
SLICE_4_WORKLOADS = {
    ("traefik", "traefik"),
    ("authelia", "authelia"),
    ("crowdsec", "crowdsec"),
    ("pihole", "pihole"),
    ("pihole", "pihole-2"),
    ("mosquitto", "mosquitto"),
    ("nut", "nut"),
    ("jellyfin", "jellyfin"),
}
SLICE_4_ROLES = {role for role, _name in SLICE_4_WORKLOADS}
```

`pihole` contributes two workloads from one role — the same role/workload divergence slice 3 hit
with `claude-otel`, which is why the guard is workload-granular.

- [ ] **Step 3: Extend the expected set and run the guard**

Add `SLICE_4_ROLES` to the union in `test_exactly_the_fenced_roles_carry_the_baseline_label`.

Run: `uv run pytest ansible/tests/test_netpol_baseline_labels.py -q`
Expected: FAIL, listing the slice-4 roles as `missing` — they are not labelled yet.

- [ ] **Step 4: Commit the failing guard**

```bash
git add ansible/tests/test_netpol_baseline_labels.py
git commit -m "netpol slice 4: extend the label guard before labelling anything"
```

Committing a red guard is deliberate: it is the executable definition of what this slice must
achieve, and every later task moves it toward green.

---

## Task 2: The traefik policy

**Files:**
- Create: `ansible/roles/k8s/netpol-baseline/templates/networkpolicy-traefik.yaml.j2`
- Modify: `ansible/roles/k8s/netpol-baseline/tasks/main.yml`

**Interfaces:**
- Consumes: `netpol_baseline_enforced` (already `true`), `k8s_namespace`.

**This is the highest-consequence object in the whole rollout.** Getting it wrong takes down every
route in the cluster, which is why the spec put it last. `:80`/`:443` stay open to all sources —
narrowing buys nothing when they are reachable from the whole network today, and with ETP=Local the
real client IP is preserved so external traffic arrives as `10.0.0.x`, not a node IP.

- [ ] **Step 1: Write the policy**

```jinja
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: traefik
  namespace: {{ k8s_namespace }}
spec:
  podSelector:
    matchLabels:
      app: traefik
  policyTypes:
    - Ingress
  ingress:
    # The front door. Open to every source on purpose: these ports already take LAN, WireGuard
    # and Cloudflare traffic, and ETP=Local preserves the client IP, so an ipBlock list here
    # would have to enumerate the internet. It also keeps uptime-kuma's ~20 monitors and all
    # four netpol probe control legs working -- each dials traefik:80 to prove the probe itself
    # has a network.
    - ports:
        - port: 80
          protocol: TCP
        - port: 443
          protocol: TCP
    # :8080 is the port worth fencing. prometheus moved to the observability namespace in
    # slice 3, so this is cross-namespace: namespaceSelector and podSelector are siblings in
    # ONE from item, which ANDs them. As separate items they would OR, admitting every pod in
    # observability and every prometheus anywhere.
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: observability
          podSelector:
            matchLabels:
              app: prometheus
      ports:
        - port: 8080
          protocol: TCP
```

- [ ] **Step 2: Render and validate against the live API server**

```bash
uv run python scripts/validate/validate_k8s_manifests.py
./scripts/deploy.sh --tags netpol-baseline --dry-run
```
Expected: both pass. The dry run applies with `--dry-run=server`, catching bad apiVersions and
field names that local YAML parsing cannot.

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/k8s/netpol-baseline/templates/networkpolicy-traefik.yaml.j2 \
        ansible/roles/k8s/netpol-baseline/tasks/main.yml
git commit -m "netpol slice 4: traefik policy -- open web ports, fenced metrics port"
```

---

## Task 3: The authelia and crowdsec policies

**Files:**
- Create: `ansible/roles/k8s/netpol-baseline/templates/networkpolicy-authelia.yaml.j2`
- Create: `ansible/roles/k8s/netpol-baseline/templates/networkpolicy-crowdsec.yaml.j2`
- Modify: `ansible/roles/k8s/netpol-baseline/tasks/main.yml`

Both are Traefik-fed, which is why they share a task: a reviewer judging one judges the other.

Authelia's only in-cluster caller is the Traefik pod. Every OIDC-looking reference elsewhere in the
repo is an IngressRoute attaching the **middleware**, which Traefik executes — the app itself never
dials authelia. Verified by resolving `forwardauth-middleware.yaml.j2:11`:
`http://authelia.{{ k8s_namespace }}.svc.cluster.local:{{ container_item.port }}/api/authz/forward-auth`.

- [ ] **Step 1: Write the authelia policy**

```jinja
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: authelia
  namespace: {{ k8s_namespace }}
spec:
  podSelector:
    matchLabels:
      app: authelia
  policyTypes:
    - Ingress
  ingress:
    # Traefik is the only in-cluster caller: forwardauth-middleware.yaml.j2 dials
    # authelia.<ns>.svc:9091 from the Traefik pod. Apps behind the middleware never dial
    # authelia themselves -- an Authelia 302 is issued by Traefik before it proxies.
    - from:
        - podSelector:
            matchLabels:
              app: traefik
      ports:
        - port: 9091
          protocol: TCP
```

- [ ] **Step 2: Write the crowdsec policy**

```jinja
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: crowdsec
  namespace: {{ k8s_namespace }}
spec:
  podSelector:
    matchLabels:
      app: crowdsec
  policyTypes:
    - Ingress
  ingress:
    # LAPI (8080) and AppSec (7422): the Traefik bouncer plugin, plus the node agents that ship
    # parsed logs to the LAPI. The dashboard (3000) is reached through Traefik like any route.
    - from:
        - podSelector:
            matchLabels:
              app: traefik
        - podSelector:
            matchLabels:
              app: crowdsec-node-agent
      ports:
        - port: 8080
          protocol: TCP
        - port: 7422
          protocol: TCP
        - port: 3000
          protocol: TCP
    # Metrics, cross-namespace to observability -- siblings in one from item, see the traefik
    # policy's comment for why that distinction is load-bearing.
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: observability
          podSelector:
            matchLabels:
              app: prometheus
      ports:
        - port: 6060
          protocol: TCP
```

- [ ] **Step 3: Confirm the node agents' label before trusting the selector**

```bash
kubectl -n homelab get ds crowdsec-node-agent -o jsonpath='{.spec.template.metadata.labels}'
```
Expected: `{"app":"crowdsec-node-agent"}` — verified 2026-08-19, so this is a re-check, not a
discovery. Re-run it anyway: a podSelector that matches nothing fails silently, cutting the agents
off from the LAPI with no error anywhere, and a label change between planning and execution is
exactly the drift this catches.

- [ ] **Step 4: Validate and commit**

```bash
./scripts/deploy.sh --tags netpol-baseline --dry-run
git add ansible/roles/k8s/netpol-baseline/templates/networkpolicy-authelia.yaml.j2 \
        ansible/roles/k8s/netpol-baseline/templates/networkpolicy-crowdsec.yaml.j2 \
        ansible/roles/k8s/netpol-baseline/tasks/main.yml
git commit -m "netpol slice 4: authelia and crowdsec policies"
```

---

## Task 4: The pihole policy — the DNS blast radius

**Files:**
- Create: `ansible/roles/k8s/netpol-baseline/templates/networkpolicy-pihole.yaml.j2`
- Modify: `ansible/roles/k8s/netpol-baseline/tasks/main.yml`

**Read the census section 2 before writing this.** The spec claims pihole's `:53` callers are hosts
rather than pods; that is false. CoreDNS forwards to `10.43.252.187` — `pihole-dns`'s ClusterIP —
so cluster DNS arrives from a CoreDNS pod in `kube-system`. Omitting that peer breaks DNS
cluster-wide, and every symptom will look like something else.

- [ ] **Step 1: Re-verify the forward target on the live cluster**

```bash
kubectl -n kube-system get cm coredns -o jsonpath='{.data.Corefile}' | grep -E '^\s*forward \.'
kubectl -n homelab get svc pihole-dns -o jsonpath='{.spec.clusterIP}'
```
Expected: the first prints a forward line whose first address equals the second. If they no longer
match, the CoreDNS peer may be unnecessary — but confirm where DNS *does* go before dropping it.

- [ ] **Step 2: Write the policy**

```jinja
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: pihole
  namespace: {{ k8s_namespace }}
spec:
  # `app: pihole` deliberately, NOT the Service's two-label selector. There are two instances --
  # pihole (instance=pihole) and pihole-2 (instance=pihole-2) -- and the single label covers both.
  # The `pihole` Service selects only instance=pihole; matching the Service here would fence one
  # replica of an HA pair and leave the other wide open.
  podSelector:
    matchLabels:
      app: pihole
  policyTypes:
    - Ingress
  ingress:
    # :53 stays open. It is daniel-server's resolv.conf nameserver and serves every LAN client,
    # whose addresses are not enumerable here. The UI is the attack surface; :53 is what takes
    # the house down.
    #
    # Cluster DNS also lands here: CoreDNS forwards to pihole-dns's ClusterIP, so these queries
    # arrive from a kube-system pod, not from a node. An open :53 covers that too -- it is
    # recorded because the spec asserted the opposite, and a future narrowing must not repeat it.
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
    # The admin UI, fenced to Traefik and the homepage widget.
    - from:
        - podSelector:
            matchLabels:
              app: traefik
        - podSelector:
            matchLabels:
              app: homepage
      ports:
        - port: 80
          protocol: TCP
```

- [ ] **Step 3: Validate and commit**

```bash
./scripts/deploy.sh --tags netpol-baseline --dry-run
git add ansible/roles/k8s/netpol-baseline/templates/networkpolicy-pihole.yaml.j2 \
        ansible/roles/k8s/netpol-baseline/tasks/main.yml
git commit -m "netpol slice 4: pihole policy -- :53 open, UI fenced to traefik"
```

---

## Task 5: mosquitto and nut — observe the source before writing the selector

**Files:**
- Create: `ansible/roles/k8s/netpol-baseline/templates/networkpolicy-mosquitto.yaml.j2`
- Create: `ansible/roles/k8s/netpol-baseline/templates/networkpolicy-nut.yaml.j2`
- Modify: `ansible/roles/k8s/netpol-baseline/tasks/main.yml`

The spec is explicit that mosquitto's source address must be **observed, not assumed**: zigbee2mqtt
reaches it through the MetalLB VIP `10.0.0.242` rather than the ClusterIP, and with ETP=Local the
source may be the pod IP or may be SNAT'd to a node address. Home Assistant also reaches it, and its
broker address lives in HA's `.storage` — invisible to grep, so do not conclude from the repo that
HA is not a caller.

- [ ] **Step 1: Observe both sources before writing either selector**

```bash
kubectl -n homelab logs deploy/mosquitto --since=30m | grep -iE 'new client|connected' | tail -20
```
Record the source addresses. A `10.42.x.y` is a pod IP (use a podSelector); a `10.42.0.1` /
`10.42.1.1` is a node bridge address (use an ipBlock). **Write down which you saw** — this
observation is the task's real deliverable and the ledger must carry it.

- [ ] **Step 2: Write the mosquitto policy admitting BOTH forms until the observation is conclusive**

```jinja
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: mosquitto
  namespace: {{ k8s_namespace }}
spec:
  podSelector:
    matchLabels:
      app: mosquitto
  policyTypes:
    - Ingress
  ingress:
    # zigbee2mqtt and home-assistant, admitted as pods AND as node addresses. Callers arrive
    # through the MetalLB VIP 10.0.0.242, and with ETP=Local the source is the pod IP when the
    # caller is in-cluster but may be SNAT'd to the node bridge. Both forms are admitted until
    # step 1's observation proves which occurs; narrowing needs a caller census first, because
    # two of three previous ClientIP narrowings in this homelab broke callers hiding in the
    # CIDR they were narrowing against.
    - from:
        - podSelector:
            matchLabels:
              app: zigbee2mqtt
        - podSelector:
            matchLabels:
              app: home-assistant
        - ipBlock:
            cidr: 10.42.0.1/32
        - ipBlock:
            cidr: 10.42.1.1/32
      ports:
        - port: 1883
          protocol: TCP
```

- [ ] **Step 3: Write the nut policy**

```jinja
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: nut
  namespace: {{ k8s_namespace }}
spec:
  podSelector:
    matchLabels:
      app: nut
  policyTypes:
    - Ingress
  ingress:
    # peanut is the web UI in-cluster. The node entries are daniel-server's shutdown chain:
    # upsmon runs as a host process there, so it arrives as a node address, not a pod. Both
    # nodes' cni0 and flannel.1 are admitted because nut has no node pin -- if the pod moves to
    # daniel-server the same caller becomes same-node instead of cross-node, and the address
    # changes with it.
    - from:
        - podSelector:
            matchLabels:
              app: peanut
        - ipBlock:
            cidr: 10.42.0.1/32
        - ipBlock:
            cidr: 10.42.1.1/32
        - ipBlock:
            cidr: 10.42.0.0/32
        - ipBlock:
            cidr: 10.42.1.0/32
      ports:
        - port: 3493
          protocol: TCP
```

- [ ] **Step 4: Validate and commit**

```bash
./scripts/deploy.sh --tags netpol-baseline --dry-run
git add ansible/roles/k8s/netpol-baseline/templates/networkpolicy-mosquitto.yaml.j2 \
        ansible/roles/k8s/netpol-baseline/templates/networkpolicy-nut.yaml.j2 \
        ansible/roles/k8s/netpol-baseline/tasks/main.yml
git commit -m "netpol slice 4: mosquitto and nut policies, both peer forms admitted"
```

---

## Task 6: The jellyfin policy

**Files:**
- Create: `ansible/roles/k8s/netpol-baseline/templates/networkpolicy-jellyfin.yaml.j2`
- Modify: `ansible/roles/k8s/netpol-baseline/tasks/main.yml`

Jellyfin moved here from slice 2. It has three in-cluster callers and a LAN VIP (`jellyfin-lan`,
`10.0.0.241`) that TV clients dial directly, bypassing Traefik.

- [ ] **Step 1: Write the policy**

```jinja
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: jellyfin
  namespace: {{ k8s_namespace }}
spec:
  podSelector:
    matchLabels:
      app: jellyfin
  policyTypes:
    - Ingress
  ingress:
    # :8096 stays open to all sources. LAN clients (TVs, phones) reach the jellyfin-lan VIP
    # 10.0.0.241 directly rather than through Traefik, and with ETP=Local they arrive as their
    # own 10.0.0.x address -- enumerable only as "the LAN", which buys nothing over leaving it
    # open. traefik, homepage and janitorr all reach the same port and are covered by this.
    - ports:
        - port: 8096
          protocol: TCP
```

- [ ] **Step 2: Validate and commit**

```bash
./scripts/deploy.sh --tags netpol-baseline --dry-run
git add ansible/roles/k8s/netpol-baseline/templates/networkpolicy-jellyfin.yaml.j2 \
        ansible/roles/k8s/netpol-baseline/tasks/main.yml
git commit -m "netpol slice 4: jellyfin policy"
```

---

## Task 7: Label the workloads

**Files:**
- Modify: `ansible/roles/k8s/{traefik,authelia,crowdsec,pihole,mosquitto,nut,jellyfin}/templates/*.yaml.j2`
- Modify the three from Task 1's decision, if it cleared them.

**This is the task that arms every fence in the slice.** `netpol_baseline_enforced` is already
`true`, so a labelled pod is fenced the moment it rolls — there is no second flag. Slice 3 had one
and this does not; do not carry the assumption across.

- [ ] **Step 1: Add the label to each workload's pod template**

```yaml
  template:
    metadata:
      labels:
        app: traefik
        netpol-baseline: enforced
```

Add it under `spec.template.metadata.labels` **only**. Never touch `spec.selector.matchLabels` — a
Deployment's selector is immutable and changing it makes the apply fail.

- [ ] **Step 2: Run the guard — it must now be green**

Run: `uv run pytest ansible/tests/test_netpol_baseline_labels.py -q`
Expected: PASS. Task 1 committed this red; this is where it goes green.

- [ ] **Step 3: Run the whole suite and commit**

```bash
uv run pytest -q
git add ansible/roles/k8s
git commit -m "netpol slice 4: label the infra-tier workloads"
```

---

## Task 8: The slice-4 probe

**Files:**
- Create: `ansible/roles/k8s/netpol-baseline/templates/netpol-probe-slice4-job.yaml.j2`
- Modify: `ansible/roles/k8s/netpol-baseline/tasks/main.yml`

A probe that only shows admitted callers still work proves nothing about the fence — the target
count returning to baseline would look identical if no policy had applied. The inverted assertions
are the proof.

- [ ] **Step 1: Write the Job**

```jinja
apiVersion: batch/v1
kind: Job
metadata:
  name: netpol-baseline-probe-slice4
  namespace: {{ k8s_namespace }}
spec:
  activeDeadlineSeconds: 240
  backoffLimit: 0
  template:
    metadata:
      labels:
        app: netpol-probe-slice4
    spec:
      restartPolicy: Never
      containers:
        - name: probe
          image: {{ netpol_baseline_probe_image }}
          command:
            - /bin/sh
            - -c
            - |
              set -u
              # CONTROL. This pod carries no label any slice-4 policy admits, so it must still
              # reach traefik:80 -- those ports are open to all sources by design. A failure
              # here means the probe itself has no network, and every assertion below would be
              # a false pass.
              if ! nc -w 5 -z traefik 80; then
                echo "CONTROL FAILED: traefik:80 unreachable -- assertions below are meaningless"
                exit 1
              fi
              echo "control ok: traefik:80 reachable"

              # INVERTED. Each must FAIL. An unlisted caller reaching these means the fence is
              # not doing its job.
              for target in authelia:9091 crowdsec:8080 nut:3493 mosquitto:1883; do
                host=${target%%:*}
                port=${target##*:}
                if nc -w 5 -z "$host" "$port"; then
                  echo "LEAK: $target reachable from an unlisted caller"
                  exit 1
                fi
                echo "isolated: $target unreachable from an unlisted caller"
              done

              # DNS must still work -- this is the CoreDNS -> pihole path the census found.
              # Resolving anything at all proves it, since cluster DNS forwards through pihole.
              if ! nslookup traefik.{{ k8s_namespace }}.svc.cluster.local >/dev/null 2>&1; then
                echo "DNS FAILED: cluster resolution broke -- check the pihole policy"
                exit 1
              fi
              echo "dns ok: cluster resolution works through the pihole fence"
```

- [ ] **Step 2: Validate and commit**

```bash
./scripts/deploy.sh --tags netpol-baseline --dry-run
git add ansible/roles/k8s/netpol-baseline/templates/netpol-probe-slice4-job.yaml.j2 \
        ansible/roles/k8s/netpol-baseline/tasks/main.yml
git commit -m "netpol slice 4: the infra-tier fence probe"
```

---

## Task 9: Deploy, then record what it settled

**Files:**
- Modify: `docs/networkpolicy-default-deny.md`

**Deploy order matters and is not the slice-3 order.** Deploy `netpol-baseline` **before** the
workload roles, so every policy exists before any pod carries the baseline label. The reverse
order fences pods against policies that do not exist yet.

**Never run a tagless `./scripts/deploy.sh` until slice 4 is fully deployed.** `containers_list`
in `ansible/inventory/host_vars/daniel-box.yml` has no toposort and runs in list order. `traefik`
is entry **2** of 52 k8s entries (line 78); `netpol-baseline` is entry **52**, the last one
(line 653). A tagless run therefore puts the `netpol-baseline: enforced` label on the Traefik pod
50 entries *before* the policies apply. In that window the already-live baseline policy selects
Traefik and admits only the Traefik pod, Prometheus and the node CIDRs — while LAN clients arrive
as their own `10.0.0.x` addresses under ETP=Local. The edge dies, and it takes with it the routes
you would use to diagnose it. This is an operator footgun only: `gitops-deploy` defers k8s role
changes, so the automated pipeline cannot reach it on its own.

- [ ] **Step 1: Capture witnesses before anything deploys**

```bash
uv run python scripts/diagnostics/probe.py metric 'count(up == 1)'
uv run python scripts/diagnostics/probe.py health monitor-bridge
```
Record the scrape-target count as the baseline. Kuma is the out-of-band witness.

- [ ] **Step 2: Apply the policies — THIS IS THE STEP THAT ARMS ALL SEVEN FENCES**

```bash
./scripts/deploy.sh --tags netpol-baseline
```

**This step is the dangerous one, not Step 3.** A NetworkPolicy makes a pod default-deny by
SELECTING it, and each of the seven bespoke policies selects on the workload's own `app:` label —
`app: traefik`, `app: nut`, and so on. Those labels are already on the pods. So the moment this
command applies, every one of the seven workloads is default-deny and admits only what its own
policy names. Nothing waits for the `netpol-baseline: enforced` label; that label adds the
baseline's admissions on top, in Step 3.

Watch `nut` hardest here. Its policy is the narrowest and it has two live callers that only a
podSelector admits — Home Assistant's NUT integration (`sensor.ups_power`, the Energy dashboard
and the battery automations) and uptime-kuma's `k3s nut (upsd)` monitor. `traefik`, by contrast,
is the *safe* one at this step: its own policy opens `:80`/`:443` to every source, so applying it
changes nothing an LAN or WAN client can see.

If this step breaks something, `./scripts/deploy.sh --tags netpol-baseline -e
netpol_baseline_enforced=false` is the undo: every slice-4 policy re-renders as allow-all
(`ingress: [{}]`) and re-applies over the fenced version. Deleting the policies is not available —
`kubectl delete networkpolicy` is deny-listed for agents and refused by the readonly SA.

- [ ] **Step 3: Label the workloads, one role at a time, traefik LAST**

```bash
./scripts/deploy.sh --tags authelia
./scripts/deploy.sh --tags crowdsec
./scripts/deploy.sh --tags pihole
./scripts/deploy.sh --tags mosquitto
./scripts/deploy.sh --tags nut
./scripts/deploy.sh --tags jellyfin
./scripts/deploy.sh --tags traefik
```

One at a time so a break is attributable to one workload. This step adds the baseline's
admissions (Traefik, Prometheus, the node CIDRs) to each pod; it does not create the fence, which
Step 2 already did. **traefik last** all the same: a mistake in the baseline's own peer set takes
out every route in the cluster at once, including the routes you would use to diagnose the others.
After `pihole`, before continuing, confirm DNS still resolves — that is the cluster-wide one:

```bash
uv run python scripts/diagnostics/probe.py metric 'count(up == 1)'
```

- [ ] **Step 4: Verify**

- Probe Job green, including its DNS leg.
- Scrape-target count back at the Step 1 baseline.
- `uv run python scripts/diagnostics/probe.py health monitor-bridge` and its log showing `cluster_targets`.
- Grafana reachable through Traefik — and remember a 302 proves nothing, since Authelia's redirect
  fires in the middleware before Traefik proxies. Use a route without Authelia to prove the backend.
- uptime-kuma's monitors still green — the ~20 `*.local.` checks are the broadest live evidence
  that Traefik's front door stayed open.

> **The undo lever is wider than this slice.** `netpol_baseline_enforced` gates
> `networkpolicy.yaml.j2:32` as well, so `-e netpol_baseline_enforced=false` does not just disarm
> slice 4 — it renders `baseline-ingress` allow-all too, unfencing the **16 slice-1 and slice-2
> workloads** that rely on it. Slice 3 is unaffected; it has its own flag. If you need to unstick
> one slice-4 workload mid-deploy, prefer reverting that workload's label to disarming the whole
> baseline.
>
> **`--tags pihole` rolls both DNS pods.** The role deploys pihole and pihole-2, each
> `strategy: Recreate` with one replica, so each rollout empties that pod's slot in the
> `pihole-dns` VIP backend. Cluster pods have the Cloudflare fallback on CoreDNS's forward line;
> **LAN clients pointed at 10.0.0.243 do not.** Expect a possible DNS gap for the LAN at that step
> and verify resolution after it.

- [ ] **Step 5: Record what the deploy settled, and define slice 4.5**

Add an *Answers from slice 4* section to the spec covering: whether mosquitto's source was the pod
IP or a node address (Task 5 step 1), whether the CoreDNS peer proved necessary, and the deploy
order finding above. Correct the spec's false ":53 callers are hosts, not pods" claim in place —
leaving it would send the next reader down the same path.

Then add **slice 4.5** to the slice plan: the 28 workloads belonging to no slice, in the four
groups the census found — 13 standalone apps, 7 sub-workloads inside a parent role, 4 DaemonSets
(fence or exempt), and terraria/valheim with their stats sidecars (LAN VIP peers, deliberately
deferred since slice 1). State plainly that **slice 5 is blocked until slice 4.5 lands**, because
slice 5's gate fails on any unlabelled pod.

```bash
git add docs/networkpolicy-default-deny.md
git commit -m "netpol slice 4: record what the deploy settled, define slice 4.5"
```

---

## Self-review

**Spec coverage.** The spec's *Slice 4 specifics* has three bullets: pihole `:53` open with the UI
fenced (Task 4), mosquitto observed-not-assumed (Task 5 step 1), three host crons dialling Traefik
needing ipBlocks (Task 2 — subsumed, since `:80`/`:443` are open to all sources, so the crons need
no enumerated peer). *Traefik's own ports stay open* is Task 2. Jellyfin's move from slice 2 is
Task 6. The registry/headlamp/n8n label-only status is Task 1.

**Gap found and closed.** The spec assumes slice 4 completes workload coverage; it does not. Task 9
step 5 defines slice 4.5 rather than leaving the slice plan overstating itself.

**Correction to the spec, carried in Task 9 step 5.** The ":53 callers are hosts, not pods" claim is
false — CoreDNS is a pod caller. Recorded in the census section and fixed in the spec at the end.

**Placeholder scan.** No TBDs. Every policy is a complete manifest; the one genuinely unknown value
(mosquitto's source form) is handled by admitting both forms and observing, which is the spec's own
instruction, not a deferral.

**Consistency.** `SLICE_4_WORKLOADS` / `SLICE_4_ROLES` are defined in Task 1 and used in Task 7.
Policy file names in the File structure table match those created in Tasks 2–6 and 8.
