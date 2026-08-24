# NetworkPolicy Slice 4.5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to
> implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fence the 27 `homelab` controllers that no slice owns, so slice 5 can flip
`netpol_baseline_scope` to `namespace` without failing its gate on unlabelled pods.

**Architecture:** Every workload gets `netpol-baseline: enforced` on its pod template, which
puts it under the existing `baseline-ingress` policy (traefik + prometheus + the two cni0 /32s).
A workload with callers outside that set gets an additive per-workload policy in
`ansible/roles/k8s/netpol-baseline/templates/`, exactly as slices 1-4 did.

**Tech Stack:** k3s, kube-router (ingress only), Ansible, Jinja2 manifests.

**Spec:** `docs/networkpolicy-default-deny.md` (see its "Slice 4.5" section).

## Global Constraints

- **Ingress only.** kube-router selects pods for an Egress policy and blocks nothing. Never
  write one.
- **A `ports:` entry is the POD's port, not the Service's.** Verified 2026-08-20 for all 27:
  every Service in scope maps port 1:1 to `targetPort`. Traefik's 80->8000 / 443->8443
  divergence does not recur here — but re-resolve `targetPort` for anything this plan adds.
- **A sidecar carries its host pod's labels.** `uptime-kuma` runs an `autokuma` sidecar; it
  reaches Kuma over pod-local loopback, which no policy evaluates. No other workload in scope
  has a sidecar (verified against `.spec.containers[*].name` for every pod, 2026-08-20).
- **Every policy renders allow-all under `{% else %}`**, never a skipped task: `kubectl apply`
  does not prune, so `netpol_baseline_enforced: false` must produce `- {}` for the flag to be a
  real rollback lever.
- `{% if netpol_baseline_enforced | bool %}` — the `| bool` is load-bearing against a string
  `-e` override.
- **`headlamp`, `n8n`, `registry` and `flaresolverr` are out of scope.** They are unlabelled but
  already born-fenced by their own policies. Labelling them would ADD the baseline's allow-list
  on top, widening them. Slice 5 inherits that problem; this slice must not create it early.

## The caller matrix

Derived 2026-08-20 from the live cluster plus a caller-side read of the four fan-out callers
(`uptime-kuma/templates/static-monitors.yaml.j2`, `homepage/templates/config/`,
`monitor-bridge/files/check.py` + `env-secret.yaml.j2`, `autofix-bridge/files/autofix.py`).
"baseline only" means traefik + prometheus + node CIDRs covers every caller.

| Workload | Pod port | Callers beyond the baseline |
|---|---|---|
| cloudflare-ddns-direct | none | — (baseline only, label only) |
| cloudflare-ddns-proxied | none | — |
| karakeep-time-tagger | none | — |
| n8n-runners | none | — |
| scrutiny-collector (DS) | none | — |
| node-exporter (DS) | 9100 | — (prometheus) |
| promtail (DS) | 9080 | — (prometheus) |
| homepage | 3000 | — |
| homelab-mcp | 8000 | — |
| freshrss | 80 | — |
| karakeep | 3000 | — |
| livesync | 5984 | — |
| peanut | 8080 | — |
| zigbee2mqtt | 8080 | — |
| terraria-stats | 9420 | — |
| valheim-stats | 9420 | — |
| karakeep-chrome | 9222 | `app: karakeep` |
| karakeep-meilisearch | 7700 | `app: karakeep` |
| freshrss-feed-cache | 80 | `app: freshrss` |
| scrutiny-influxdb | 8086 | `app: scrutiny-web` |
| scrutiny-web | 8080 | `app: scrutiny-collector`, `app: monitor-bridge` |
| home-assistant | 8123 | `app: homelab-mcp`, `app: monitor-bridge` |
| uptime-kuma | 3001 | `app: monitor-bridge`, `app: autofix-bridge`, `app: cloudflare-ddns-direct`, `app: cloudflare-ddns-proxied` |
| loki-homelab | 3100 | `app: promtail`, `app: homelab-mcp`, `app: monitor-bridge`, `app: terraria-stats`, `app: valheim-stats`, and `app: grafana` in the `observability` namespace |
| terraria | 7777 | LAN game clients over a `LoadBalancer` with `externalTrafficPolicy: Local` (client IP preserved, not enumerable) + the Kuma "k3s Terraria (game port)" monitor via the node IP |
| valheim | 2456, 2457 | same, UDP |
| wg-easy | 51820 (UDP), 51821 | 51820 is the VPN listener behind a `Local` LoadBalancer; 51821 is the UI, baseline only |

## The probe control problem — settle this before anything else

Slices 1 and 2's probe jobs use `homepage:3000` as an **unfenced negative control**: it proves
the probe pod has network and DNS, so a passing inverted leg passed for the right reason.
Labelling homepage removes the last unfenced pod in `homelab`, and slice 5 (`podSelector: {}`)
removes the possibility of ever having one again.

**Ruling: replace the unfenced control with a designated sentinel.** `littlelink` is a static
link page on :3000 with no data and no dependents. A new policy admits any pod carrying
`netpol-probe: control` to `app: littlelink` on 3000. Probe pods carry that label; the control
leg dials `littlelink:3000`. This proves the same thing the old control proved, is explicit
rather than incidental, and survives slice 5 unchanged.

---

### Task 1: The sentinel control, and the four existing probes

**Files:**
- Create: `ansible/roles/k8s/netpol-baseline/templates/networkpolicy-netpol-sentinel.yaml.j2`
- Modify: `ansible/roles/k8s/netpol-baseline/tasks/main.yml` (render + apply the new template)
- Modify: `ansible/roles/k8s/netpol-baseline/templates/netpol-probe-job.yaml.j2`,
  `netpol-probe-slice2-job.yaml.j2`, `netpol-probe-slice3-job.yaml.j2`,
  `netpol-probe-slice4-job.yaml.j2`

**Interfaces:**
- Produces: the pod label `netpol-probe: control`, and a sentinel target `littlelink:3000`.
  Every later probe in this plan uses both.

- [ ] **Step 1: Write the sentinel policy.** Same `{% if netpol_baseline_enforced | bool %}` /
      `{% else %}` `- {}` shape as `networkpolicy-pihole.yaml.j2`. Selector `app: littlelink`;
      one rule, `from: [podSelector: {matchLabels: {netpol-probe: control}}]`,
      `ports: [{port: 3000, protocol: TCP}]`. Comment why it exists: it is the probes' positive
      control, deliberately explicit because slice 5 leaves no unfenced pod.
- [ ] **Step 2: Wire it into `tasks/main.yml`** next to the other per-workload policies.
- [ ] **Step 3: Add `netpol-probe: control`** to the pod labels of all four existing probe Jobs.
- [ ] **Step 4: Repoint slices 1 and 2's control leg** from `homepage:3000` to `littlelink:3000`,
      and update the two `NEGATIVE CONTROL FAILED: cannot reach the unfenced homepage:3000`
      strings — the target is no longer "unfenced", it is "admitted by the sentinel policy".
- [ ] **Step 5:** `prek run --all-files`, then commit.

---

### Task 2 (Stage A): Label the workloads nothing dials

**Files:** the `deployment.yaml.j2` / `daemonset.yaml.j2` of `cloudflare-ddns` (both templates),
`karakeep` (time-tagger), `n8n-runners`, `scrutiny` (collector-daemonset), `node-exporter`,
`loki-homelab` (promtail daemonset).

- [ ] **Step 1:** Add `netpol-baseline: enforced` under `spec.template.metadata.labels` in each.
      One line per file. Nothing else changes.
- [ ] **Step 2:** `prek run --all-files`; commit.

Batch this as one dispatch — seven identical one-line edits.

---

### Task 3 (Stage A): The five sub-workload policies

**Files:** create
`networkpolicy-{karakeep-chrome,karakeep-meilisearch,freshrss-feed-cache,scrutiny-influxdb,scrutiny-web}.yaml.j2`
in `ansible/roles/k8s/netpol-baseline/templates/`, wire each into `tasks/main.yml`, and add the
label to each workload's pod template.

Peers and ports come from the caller matrix above, verbatim. `scrutiny-web`'s Service is named
`scrutiny`, not `scrutiny-web` — the selector is `app: scrutiny-web`.

- [ ] **Step 1:** Write the five policies, each with the standard enforced/allow-all wrapper.
- [ ] **Step 2:** Wire them into `tasks/main.yml`.
- [ ] **Step 3:** Label the five pod templates.
- [ ] **Step 4:** `prek run --all-files`; commit.

---

### Task 4 (Stage B): Label the nine standalone apps with baseline-only callers

**Files:** the pod templates of `homepage`, `homelab-mcp`, `freshrss`, `karakeep`, `livesync`,
`peanut`, `zigbee2mqtt`, `terraria-stats`, `valheim-stats`.

- [ ] **Step 1:** Add `netpol-baseline: enforced` to each pod template.
- [ ] **Step 2:** `prek run --all-files`; commit.

Homepage's own outbound widget calls are unaffected — this policy is ingress-only, and every
widget except `http://pihole:80` goes out through Traefik anyway.

---

### Task 5 (Stage B): The three fan-in policies

**Files:** create `networkpolicy-{home-assistant,uptime-kuma,loki-homelab}.yaml.j2`; wire into
`tasks/main.yml`; label the three pod templates.

`loki-homelab` is the only cross-namespace case: `app: grafana` lives in `observability`, so its
peer is a single list item with `namespaceSelector` AND `podSelector` as sibling keys — as two
items they would OR and admit the whole namespace.

- [ ] **Step 1:** Write the three policies from the caller matrix.
- [ ] **Step 2:** Wire and label.
- [ ] **Step 3:** `prek run --all-files`; commit.

---

### Task 6 (Stage C): The three LAN-facing workloads

**Files:** create `networkpolicy-{terraria,valheim,wg-easy}.yaml.j2`; wire; label.

All three sit behind a `LoadBalancer` with `externalTrafficPolicy: Local`, so the LAN client IP
survives to the pod and is not enumerable. Each opens its game or VPN port to everyone — the
same shape as pihole's `:53` rule — and fences nothing else:

- terraria: TCP 7777 open. (Kuma's game-port monitor dials the node IP; the open rule covers it.)
- valheim: UDP 2456 and 2457 open.
- wg-easy: UDP 51820 open; 51821 (the UI) is baseline-only.

- [ ] **Step 1:** Write the three policies, each commenting *why* the port is open rather than
      peered — an operator narrowing it later must find the reason here.
- [ ] **Step 2:** Wire and label.
- [ ] **Step 3:** `prek run --all-files`; commit.

---

### Task 7: The slice 4.5 probe

**Files:** create `ansible/roles/k8s/netpol-baseline/templates/netpol-probe-slice45-job.yaml.j2`.

- [ ] **Step 1:** Control leg — `nc -w 5 -z littlelink 3000` must SUCCEED (the sentinel).
- [ ] **Step 2:** Inverted legs — from an unlabelled probe pod, `home-assistant:8123`,
      `uptime-kuma:3001`, `loki-homelab:3100`, `scrutiny:8080` and `karakeep-meilisearch:7700`
      must each be REFUSED. kube-router REJECTs, so expect "connection refused", not a timeout.
- [ ] **Step 3:** Open-port leg — `terraria:7777` must still SUCCEED from the same unlabelled
      pod, proving the open rules are open.
- [ ] **Step 4:** Wire into `tasks/main.yml` behind `when: netpol_baseline_enforced | bool`.
- [ ] **Step 5:** `prek run --all-files`; commit.

---

### Task 8: Extend the label guard

**Files:** `ansible/tests/test_netpol_baseline_labels.py`

- [ ] **Step 1:** Add `SLICE_45_WORKLOADS` — every (role, workload) pair this plan labels — and
      `SLICE_45_ROLES`, and add both to the two existing assertions. The slice-4 review caught
      exactly this omission, so include the sub-workloads whose role name differs from the
      workload name (`karakeep-chrome` under `karakeep`, `scrutiny-web` under `scrutiny`,
      `promtail` under `loki-homelab`).
- [ ] **Step 2:** Prove the guard: delete one label, run
      `uv run pytest ansible/tests/test_netpol_baseline_labels.py`, confirm it FAILS naming that
      workload, restore, confirm it PASSES.
- [ ] **Step 3:** Commit.

---

### Task 9: Deploy, verify functionally, and document

This is the task the slice-4 outage argues for. A probe proves denial; it cannot prove a
legitimate caller still works, because the probe pod carries its own labels.

- [ ] **Step 1:** `./scripts/deploy.sh --tags netpol-baseline --dry-run`.
- [ ] **Step 2:** Deploy Stage A (tasks 2-3), then confirm: `scrutiny-web` lists disks,
      karakeep search returns results, freshrss loads.
- [ ] **Step 3:** Deploy Stage B (tasks 4-5), then confirm: every homepage widget renders, all
      Kuma tiles green, `probe.py ha state` answers, Grafana's loki-homelab datasource returns
      logs, monitor-bridge's next run is clean.
- [ ] **Step 4:** Deploy Stage C (task 6), then confirm: the terraria and valheim Kuma port
      tiles stay green and a WireGuard client still handshakes.
- [ ] **Step 5:** Run all five probes together; all must pass.
- [ ] **Step 6:** Confirm the labelled-pod count matches the guard's list exactly.
- [ ] **Step 7:** Add an "Answers from slice 4.5" section to `docs/networkpolicy-default-deny.md`,
      correcting its Slice 4.5 table (headlamp/n8n/registry are born-fenced and out of scope;
      the count is 27, not 28) and recording what slice 5 inherits: the born-fenced widening
      problem, and the fact that transient Job pods (buildkit builds, the probes themselves,
      `pi-peer-backup`) will be selected by `podSelector: {}`.
- [ ] **Step 8:** Commit; open the PR.
