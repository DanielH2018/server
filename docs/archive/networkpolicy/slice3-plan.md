# NetworkPolicy Slice 3 — `observability` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fence the six `observability` workloads behind ingress NetworkPolicies without blinding the telemetry the rest of the rollout is judged by.

**Architecture:** A *second* baseline NetworkPolicy object in the `observability` namespace, with its own node-CIDR list and its own enforcement flag, plus per-workload policies for the two targets that have cross-namespace callers. Labels are deployed unfenced first, then enforcement is flipped in a separate deploy.

**Tech Stack:** k3s + kube-router, Ansible `roles/k8s/netpol-baseline` and `roles/k8s/claude-otel`, pytest label guard.

**Spec:** `docs/networkpolicy-default-deny.md`

## Global Constraints

- **Ingress only.** Egress policies select pods and block nothing on this cluster (measured 2026-08-07). Never write an egress rule and never claim one enforces.
- `ingress: []` denies everything; `ingress: [{}]` allows everything. They are exact inverses.
- Within one `from` list item, sibling keys AND. Separate list items OR.
- **NetworkPolicies are namespaced.** A policy in `homelab` does not select a pod in `observability`, and a bare `podSelector` peer means *the policy's own namespace*.
- Every new var gets `| bool` on both the Jinja side and the Ansible `when:` side — the string `"false"` is truthy in Jinja and False in `when:`.
- `kubectl delete` is denied and `kubectl apply` does not prune. Rollback is a variable flip, never a hand-patch.
- Do not add to `netpol_baseline_node_cidrs`. That list is shared by the 16 already-deployed slice-1/2 workloads.

---

## What the census found

Four findings changed this plan's contents before it was written. Each is a fact measured or read this session, not an assumption.

**1. The namespace is one Ansible role.** All six workloads — grafana, prometheus, loki, tempo, kube-state-metrics, otel-collector — render from `roles/k8s/claude-otel/templates/`. There is no per-workload role to hang a policy on, and the existing label guard maps *role* → labelled, so five unlabelled workloads inside one labelled role would pass it silently. Task 1 fixes the guard before anything is labelled.

**2. Only one workload crosses into the observability Loki.** There are two Lokis. `terraria-stats`, `valheim-stats` and `monitor-bridge` all set `LOKI_URL` to `loki-homelab.{{ k8s_namespace }}` — the `homelab` one, out of scope. Only `homelab-mcp` reaches `loki.observability`, via its *second* variable `CLAUDE_LOKI_URL`. A grep for `loki:3100` finds seven roles and would have over-fenced or under-fenced depending on which hit you believed; the deciding evidence is the env value the manifest sets, not the default in the code.

**3. Neither web route is unauthenticated.** `claude-otel`'s `containers_list` entry sets `use_authelia: true` for grafana, and the prometheus IngressRoute carries its own middleware chain. So slice 3 has **no route that can serve an HTTP liveness leg** — a 302 from Authelia would print success without reaching the app, which is the trap that bit slice 1. Use slice 2's EndpointSlice readiness gate instead.

**4. The only host → ClusterIP caller is the telemetry heartbeat.** `roles/k8s/claude-otel/templates/telemetry-health.sh.j2` runs from a cron on **daniel-box alone** — `claude-otel` appears in `containers_list` only in `host_vars/daniel-box.yml:259`, nowhere in `daniel-server.yml`, and the cron task has no `delegate_to` — resolves prometheus's ClusterIP at runtime and curls `:9090`. `scripts/diagnostics/probe.py` is *not* a second one — `k8s_endpoint()` builds `https://<host>.local.<domain>` pinned to the MetalLB VIP, so it arrives through Traefik and the Traefik peer covers it.

### Why prometheus needs all four node addresses

prometheus has no `nodeSelector`, `nodeAffinity` or `nodeName` in `prometheus.yaml.j2`, and the telemetry-health cron runs from daniel-box alone (see "What the census found" above). So the heartbeat's ClusterIP curl is cross-node exactly when prometheus is scheduled onto daniel-server — unpinned, so that happens without a deploy to blame it on. By slice 2's own evidence (the `registry`/`flannel.1` finding), a cross-node host→pod packet arrives as the **sending** node's `flannel.1` address, so the caller that actually exists needs daniel-box's own flannel.1 (`10.42.0.0/32`).

That is why the observability baseline gets its **own** CIDR list containing all four addresses — both `cni0` (`10.42.0.1`, `10.42.1.1`) and both `flannel.1` (`10.42.0.0`, `10.42.1.0`). Adding `flannel.1` to the shared `netpol_baseline_node_cidrs` would re-scope all 16 deployed workloads and put slices 1 and 2 back on the review surface for a problem they do not have. `10.42.1.0/32` (daniel-server's own flannel.1) has no caller nameable today — the cron never runs there — and is kept anyway as deliberate, stated over-inclusion, at the same trust level the baseline already grants the other node bridges.

**Known imprecision, stated rather than hidden:** the flannel.1-as-cross-node-source rule itself is *strong evidence, not proof* — it rests on the registry pull argument in the spec, not a packet capture. There is independent **measured** corroboration on a different path, though: `claude-otel/templates/prometheus-ingressroute.yaml.j2:41-46` records Authelia's `remote_ip` for host-originated LAN queries to the same prometheus route — daniel-box curls arrive as `10.42.0.1` (own `cni0`, same-node) and daniel-server's as `10.42.1.0` (own `flannel.1`, cross-node). Same rule, same direction, measured on a route this heartbeat doesn't even use. Not proof — still no packet capture of the heartbeat's own traffic — but a second independent measurement, not a restatement of the same inference. If stage B fails for prometheus specifically, that inference is still the first thing to doubt.

### The otel-collector hostPort path

`collector.yaml.j2` uses `hostPort` + `hostIP` on loopback, **not** `hostNetwork`. So the pod IS selectable by `podSelector` — unlike a hostNetwork pod, which cannot be policed at all.

Each host's Claude Code exports OTLP to *its own node's* collector over `127.0.0.1:4317`, and the DaemonSet puts a collector on every node, so this path is always same-node. The post-DNAT source is expected to be that node's `cni0` address, which the baseline already admits — but this is **inference, not measurement**, and the failure mode is silent: telemetry stops and nothing turns red except `/audit-permissions` going quiet. Task 6 makes it an explicit witness rather than trusting the inference.

---

## Caller matrix

| Target | In-namespace callers | Cross-namespace callers | Host callers |
|---|---|---|---|
| `prometheus:9090` | grafana | traefik, monitor-bridge, homelab-mcp | `telemetry-health.sh`, daniel-box only |
| `loki:3100` | otel-collector (push), grafana | homelab-mcp | — |
| `tempo:3200`, `:4317` | otel-collector, grafana | — | — |
| `grafana:3000` | — | traefik | — |
| `kube-state-metrics:8080/8081` | prometheus | — | — |
| `otel-collector:4317` | — | — | node loopback via hostPort |
| `otel-collector:8889/8888` | prometheus | — | — |

Prometheus *scraping outward* needs no rule at all: that is egress from prometheus and ingress to the target, and the slice-1/2 baseline already admits `namespaceSelector: observability` + `podSelector: app=prometheus` on every fenced workload.

## File structure

| File | Responsibility |
|---|---|
| `roles/k8s/netpol-baseline/templates/networkpolicy-observability.yaml.j2` | **Create.** The observability baseline. Own CIDR list, own enforcement flag, cross-namespace traefik peer. |
| `roles/k8s/netpol-baseline/defaults/main.yml` | **Modify.** Add `netpol_baseline_obs_enforced`, `netpol_baseline_obs_node_cidrs`. |
| `roles/k8s/netpol-baseline/templates/networkpolicy-prometheus.yaml.j2` | **Create.** monitor-bridge + homelab-mcp peers. Lives in netpol-baseline, not claude-otel — see Task 4. |
| `roles/k8s/netpol-baseline/templates/networkpolicy-loki.yaml.j2` | **Create.** homelab-mcp peer. |
| `roles/k8s/claude-otel/templates/*.yaml.j2` (6 workloads) | **Modify.** Add the `netpol-baseline: enforced` pod label. |
| `roles/k8s/netpol-baseline/templates/netpol-probe-slice3-job.yaml.j2` | **Create.** Inverted probe + controls. |
| `roles/k8s/netpol-baseline/tasks/main.yml` | **Modify.** Readiness gate + probe run for slice 3. |
| `ansible/tests/k8s/test_netpol_baseline_labels.py` | **Modify.** Workload-granular guard; add `SLICE_3_WORKLOADS`. |

---

## Task 1: Make the label guard workload-granular

The guard currently answers "does this role render *any* doc carrying the label". Six workloads in one role makes that answer useless. It must answer per pod-producing document.

**Files:**
- Modify: `ansible/tests/k8s/test_netpol_baseline_labels.py:62-83`

**Interfaces:**
- Produces: `_labelled_workloads() -> set[tuple[str, str]]` — `(role, workload_name)` pairs, where `workload_name` is the doc's `metadata.name`. Tasks 3 and 7 consume this.

- [ ] **Step 1: Write the failing test**

Add alongside the existing test, keeping it:

```python
POD_KINDS = {"Deployment", "DaemonSet", "StatefulSet", "CronJob"}


def _labelled_workloads() -> set[tuple[str, str]]:
    key, value = LABEL
    return {
        (role, doc.get("metadata", {}).get("name", "?"))
        for role, _tpl, doc in rendered_docs()
        if doc.get("kind") in POD_KINDS
        and _pod_template_labels(doc).get(key) == value
    }


def test_every_pod_producing_doc_in_a_fenced_role_is_labelled() -> None:
    """A role is not a unit of fencing. claude-otel renders six workloads; five
    could go unlabelled while the role still looked fenced."""
    fenced_roles = SLICE_1_ROLES | SLICE_2_ROLES
    unlabelled = sorted(
        f"{role}/{doc.get('metadata', {}).get('name', '?')}"
        for role, _tpl, doc in rendered_docs()
        if role in fenced_roles
        and doc.get("kind") in POD_KINDS
        and _pod_template_labels(doc).get(LABEL[0]) != LABEL[1]
    )
    assert not unlabelled, (
        "pod-producing docs inside a fenced role are missing the baseline label:\n"
        f"  {unlabelled}\n"
        "Every workload in a fenced role must carry it — the role is not the unit."
    )
```

- [ ] **Step 2: Run it**

Run: `uv run pytest ansible/tests/k8s/test_netpol_baseline_labels.py -v`

Expected: PASS if slices 1–2 are internally consistent. **If it FAILS, stop and report** — that is a real unfenced workload in already-deployed code, and it is a finding, not a test bug.

- [ ] **Step 3: Commit**

```bash
git add ansible/tests/k8s/test_netpol_baseline_labels.py
git commit -F - <<'EOF'
Make the netpol label guard workload-granular, not role-granular

claude-otel renders six workloads from one role. The existing guard asks
whether a role renders any labelled doc, so five of the six could go
unlabelled while the role still read as fenced.
EOF
```

---

## Task 2: The observability baseline policy

**Files:**
- Create: `ansible/roles/k8s/netpol-baseline/templates/networkpolicy-observability.yaml.j2`
- Modify: `ansible/roles/k8s/netpol-baseline/defaults/main.yml`
- Modify: `ansible/roles/k8s/netpol-baseline/tasks/main.yml` (add to `manifests_files`)

**Interfaces:**
- Consumes: `k8s_observability_namespace`, `k8s_namespace`.
- Produces: `netpol_baseline_obs_enforced` (default **false** — Task 6 flips it), `netpol_baseline_obs_node_cidrs`.

- [ ] **Step 1: Add the defaults**

```yaml
# Slice 3 fences the observability namespace. It carries its OWN enforcement flag and its
# OWN node-CIDR list, deliberately: the shared netpol_baseline_node_cidrs is read by the 16
# workloads slices 1 and 2 already deployed, and widening it would re-scope all of them.
netpol_baseline_obs_enforced: false

# All four node addresses, where the homelab list carries only the two cni0 /32s.
# prometheus has no node pin and telemetry-health.sh curls its ClusterIP from a cron on BOTH
# nodes, so one of those two crons is cross-node whenever prometheus lands on daniel-box.
# Slice 2 measured cross-node host -> fenced pod as blocked with cni0 alone.
netpol_baseline_obs_node_cidrs:
  - 10.42.0.1/32    # daniel-box cni0
  - 10.42.1.1/32    # daniel-server cni0
  - 10.42.0.0/32    # daniel-box flannel.1
  - 10.42.1.0/32    # daniel-server flannel.1
```

- [ ] **Step 2: Write the template**

```jinja
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: netpol-baseline-observability
  namespace: {{ k8s_observability_namespace }}
spec:
  podSelector:
    matchLabels:
      netpol-baseline: enforced
  policyTypes:
    - Ingress
  ingress:
{% if netpol_baseline_obs_enforced | bool %}
    - from:
        # Traefik lives in {{ k8s_namespace }}, so unlike the homelab baseline this needs a
        # namespaceSelector: a bare podSelector peer means THIS policy's namespace.
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ k8s_namespace }}
          podSelector:
            matchLabels:
              app: traefik
        # Intra-namespace: grafana -> prometheus/loki/tempo, prometheus -> ksm/collector.
        # A bare podSelector is correct here and means observability only.
        - podSelector: {}
{% for cidr in netpol_baseline_obs_node_cidrs %}
        - ipBlock:
            cidr: {{ cidr }}
{% endfor %}
{% else %}
    - {}
{% endif %}
```

**Note the `podSelector: {}` peer.** The intra-namespace mesh here is thick and entirely trusted — every pod in `observability` is part of one telemetry system rendered from one role. Enumerating six workloads' pairwise edges would add review surface without adding a boundary; the boundary that matters is the namespace edge.

- [ ] **Step 3: Validate the render**

Run: `uv run python scripts/validate/validate_k8s_manifests.py` then `prek run --all-files`

Expected: PASS. Confirm by eye that the off-state renders `- {}` and the on-state renders four `ipBlock` entries.

- [ ] **Step 4: Commit**

---

## Task 3: Label the six workloads

**Files:**
- Modify: `ansible/roles/k8s/claude-otel/templates/{grafana,prometheus,loki,tempo,kube-state-metrics,collector}.yaml.j2`
- Modify: `ansible/tests/k8s/test_netpol_baseline_labels.py`

- [ ] **Step 1: Add `SLICE_3_WORKLOADS` to the guard, as workload names**

```python
# Slice 3: the observability namespace. Named per WORKLOAD, not per role — all six render
# from the single claude-otel role, which is why Task 1 made the guard workload-granular.
SLICE_3_WORKLOADS = {
    ("claude-otel", "grafana"),
    ("claude-otel", "prometheus"),
    ("claude-otel", "loki"),
    ("claude-otel", "tempo"),
    ("claude-otel", "kube-state-metrics"),
    ("claude-otel", "otel-collector"),
}
```

Then make two changes, and **not** a third (Ruling 1):

1. Add `"claude-otel"` to the fenced-role set that Task 1's `test_every_pod_producing_doc_in_a_fenced_role_is_labelled` iterates. That is what makes the guard demand all six.
2. Add one claude-otel-specific assertion: `{name for (role, name) in _labelled_workloads() if role == "claude-otel"} == {n for _, n in SLICE_3_WORKLOADS}`.

**Do not enumerate workload tuples for slices 1 and 2.** Task 1's test already derives that invariant from the rendered docs; restating it as a hand-maintained list would need updating on every future slice and could drift from what is actually deployed.

Run it first and watch the claude-otel assertion fail with six missing.

- [ ] **Step 2: Add `netpol-baseline: enforced` to each pod template's `metadata.labels`**

Under `spec.template.metadata.labels` for the five Deployments and the DaemonSet. Do **not** touch `spec.selector.matchLabels` — a Deployment's selector is immutable, and changing it makes the apply fail.

- [ ] **Step 3: Run the guard**

Run: `uv run pytest ansible/tests/k8s/test_netpol_baseline_labels.py -v`
Expected: PASS, 17 roles (the 16 already fenced plus `claude-otel`) + 6 observability workloads.

- [ ] **Step 4: Commit**

---

## Task 4: Per-workload policies for prometheus and loki

Only these two have cross-namespace callers. grafana is reached solely through Traefik, and tempo / kube-state-metrics / otel-collector solely from inside the namespace — all covered by Task 2's baseline.

**Files:**
- Create: `ansible/roles/k8s/netpol-baseline/templates/networkpolicy-prometheus.yaml.j2`
- Create: `ansible/roles/k8s/netpol-baseline/templates/networkpolicy-loki.yaml.j2`
- Modify: `ansible/roles/k8s/netpol-baseline/tasks/main.yml` (add both to `manifests_files`)

> **Corrected during the preflight scan (Ruling 2).** An earlier draft of this task put these two
> files in the `claude-otel` role and declared them unconditional, reasoning that "a policy which
> only adds peers is inert until something else selects the pod." **That is false.** In Kubernetes a
> NetworkPolicy selecting a pod is precisely what makes that pod default-deny. As drafted, Task 6's
> stage A would have fenced prometheus and loki the moment it deployed — admitting only
> monitor-bridge and homelab-mcp, and cutting off grafana, Traefik and the host heartbeat. That is
> the exact inversion of the two-stage deploy's purpose. Hence: netpol-baseline role, and gated on
> the same flag as the baseline.

- [ ] **Step 1: prometheus**

```jinja
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: prometheus-callers
  namespace: {{ k8s_observability_namespace }}
spec:
  podSelector:
    matchLabels:
      app: prometheus
  policyTypes:
    - Ingress
  ingress:
{% if netpol_baseline_obs_enforced | bool %}
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ k8s_namespace }}
          podSelector:
            matchExpressions:
              - key: app
                operator: In
                values: [monitor-bridge, homelab-mcp]
      ports:
        - protocol: TCP
          port: 9090
{% else %}
    - {}
{% endif %}
```

- [ ] **Step 2: loki** — same shape, `app: loki`, values `[homelab-mcp]`, port `3100`.

- [ ] **Step 3: Validate and commit**

Run: `uv run python scripts/validate/validate_k8s_manifests.py && prek run --all-files`

**Both carry the same `netpol_baseline_obs_enforced` guard as the baseline, with the same
`ingress: [{}]` off-state.** One flag governs the whole slice, so flipping it back is a true
rollback for these two policies as well — the property that was documented falsely in slice 2 and
had to be corrected afterwards.

---

## Task 5: The slice-3 probe

Model it on `netpol-probe-slice2-job.yaml.j2`. **No HTTP liveness leg** — finding 3 above.

**Files:**
- Create: `ansible/roles/k8s/netpol-baseline/templates/netpol-probe-slice3-job.yaml.j2`
- Modify: `ansible/roles/k8s/netpol-baseline/tasks/main.yml`

- [ ] **Step 1: Readiness gate**

Extend the existing EndpointSlice gate to the observability targets, same jsonpath as slice 2:

```
{.items[*].endpoints[?(@.conditions.ready==true)].targetRef.name}
```

against `prometheus`, `loki`, `grafana`, `tempo` in `{{ k8s_observability_namespace }}`. Fail closed.

- [ ] **Step 2: The probe pod**

**One Job, in `{{ k8s_namespace }}`** — the same namespace and shape as the slice-1 and slice-2 probes. An earlier draft asked for an in-namespace control *and* a cross-namespace inverted leg from one Job; a Job's pod lives in exactly one namespace, so that is not buildable (Ruling 3).

Label it `app: netpol-probe-slice3` — it must be neither `monitor-bridge` nor `homelab-mcp`, or Task 4's policies would admit it and every inverted assertion would fail.

1. **Control:** `nc -w 5 -z traefik 80` **must succeed** — proves DNS and pod networking before anything is concluded from a failure. Exactly the slice-1/2 control.
2. **Inverted assertions, both must fail to connect:**
   - `nc -w 5 -z prometheus.{{ k8s_observability_namespace }} 9090`
   - `nc -w 5 -z loki.{{ k8s_observability_namespace }} 3100`

**Why the probe cannot run from inside `observability`.** The baseline admits `podSelector: {}` — every pod in that namespace — so an in-namespace probe would *not* be blocked, and it would report the fence as broken while it is working exactly as designed. This is the single easiest way to get this slice's probe wrong.

**What this probe deliberately does not cover:** the intra-namespace `podSelector: {}` peer itself. The EndpointSlice gate proves the targets are up, and Task 6 Step 5 exercises the intra-namespace path through the real consumer — grafana rendering its panels.

- [ ] **Step 3: Gate every run task**

`when: [netpol_baseline_obs_enforced | bool, not k8s_no_mutate]` — note the **obs** flag, not the homelab one.

- [ ] **Step 4: Commit**

---

## Task 6: Two-stage deploy

**Do not label and flip in one deploy.** Labelling rolls all six pods, including loki and prometheus — both instruments. A combined deploy opens a blind window across exactly the telemetry you would use to judge the fence.

- [ ] **Step 1: Capture witnesses BEFORE anything deploys**

```bash
uv run python scripts/diagnostics/probe.py targets | tail -5
uv run python scripts/diagnostics/probe.py metric 'count(up == 1)'
uv run python scripts/diagnostics/probe.py metric 'sum(increase(otelcol_exporter_send_failed_spans[15m])) or vector(0)'
```

Record the scrape-target count and the send-failed total as the **Step 1 baseline** — every later comparison in this task chains off these numbers, not off each other. Kuma is the out-of-band witness here, and not because `probe.py alerts` is compromised by this fence — it isn't: `run_alerts` (`scripts/diagnostics/probe.py`) calls `loki_endpoint()` (moved to `scripts/diagnostics/probe_core.py` in the 2026-08-23 module split — grep the symbol, not a line), which returns `k8s_endpoint("loki-homelab")`, the **homelab** Loki, unaffected by a fence that only covers `observability`. Kuma is still the better pick because its state is independent of anything this deploy touches at all.

- [ ] **Step 2: Stage A — labels only, unfenced**

`netpol_baseline_obs_enforced` stays `false`. Deploy:

```bash
./scripts/deploy.sh --tags "claude-otel"
```

- [ ] **Step 3: Confirm recovery before fencing anything**

Re-run all three witness commands. Scrape-target count must return to its **Step 1 baseline** and `otelcol_exporter_send_failed_*` must not be climbing. **If either is off, stop** — that is the label roll, not the fence, and fencing on top of it would confuse two causes.

- [ ] **Step 4: Stage B — flip enforcement**

**Ordering hazard, check before flipping.** `netpol-baseline/tasks/main.yml` applies all four
policies — including `networkpolicy-observability.yaml` — in one `include_role` at ~line 31,
before any probe runs. The slice-1 probe and the slice-2 **hard** readiness assert (~lines
225-242) run immediately after that, still gated on `netpol_baseline_enforced` (already `true`),
and the slice-3 gate/probe does not start until ~line 370. So if sonarr, radarr, prowlarr or
qbittorrent is mid-rollout when this deploy runs, the play aborts at the slice-2 assert naming
that workload — and `observability` is *already live-fenced and completely unverified* at that
point, because the apply that fences it ran before the assert that aborted. **Confirm all four
have ready endpoints immediately before running this step** — `uv run python scripts/diagnostics/probe.py
health sonarr`, `radarr`, `prowlarr`, `qbittorrent` (each exits 0 only when the Deployment is
fully rolled out and no container restarted in the last 180s). If Step 4 or the deploy it
triggers fails for any reason, treat it as **"the fence may already be live and unverified,"**
not as "nothing happened" — check the probe output and the four witnesses before assuming the
fence never applied.

**The reassuring mirror:** because the apply precedes every probe, **rollback is safe** in exactly
the way the forward path is not. Flipping `netpol_baseline_obs_enforced` back to `false` and
redeploying renders the allow-all body regardless of whether a later probe aborts the play —
rollback does not depend on the play completing, only on the apply task running, which it always
does first.

Set `netpol_baseline_obs_enforced: true` in defaults, commit, then:

```bash
./scripts/deploy.sh --tags "netpol-baseline"
```

No pod roll this time — a NetworkPolicy change does not restart anything.

**`-e` does not persist.** `./scripts/deploy.sh --tags "netpol-baseline" -e netpol_baseline_obs_enforced=true`
is fine for a `--dry-run` pre-check, but it silently reverts on the next `netpol-baseline` deploy
— including a hand redeploy run for an unrelated reason, which would un-fence `observability`
without anyone touching this flag on purpose. Editing `defaults/main.yml` and committing is the
only durable form; that is what this step means by "Set … in defaults, commit."

- [ ] **Step 5: Verify the fence**

- Probe Job green.
- Witnesses unchanged from the **Step 1 baseline**.
- `uv run python scripts/diagnostics/probe.py health monitor-bridge` — proves the cross-namespace prometheus caller still works via the real consumer, not a probe.
- **The heartbeat:** confirm `telemetry-health.sh` succeeded. There is one pusher — daniel-box only — so check that cron's own exit there rather than trusting the monitor tile in isolation.
- Grafana loads through Traefik and its panels render (that exercises grafana → prometheus, loki and tempo in one action).

- [ ] **Step 6: Record what the deploy settled** in `docs/networkpolicy-default-deny.md` — in particular whether the hostPort DNAT source was in fact `cni0`, since that was inference.

---

## Task 7: Spec amendment

- [ ] Add a **Slice 3 specifics** section to `docs/networkpolicy-default-deny.md` mirroring the existing *Slice 4 specifics*, carrying: the two-Loki distinction, the no-unauthenticated-route consequence, the separate CIDR list and why the shared one was not widened, and the `podSelector: {}` intra-namespace decision.
- [ ] Mark slice 3 ✅ in the slice-plan table with its date.
- [ ] Update the census-method note: this slice's near-miss was **two services with the same name in different namespaces**, which a name grep cannot tell apart. Slices 4–5 must resolve the *env value the manifest sets*, not the default in the code.
- [ ] Commit.

---

## Self-review

**Spec coverage.** The spec's slice-3 row asks for four hostPort ingress paths, cross-namespace inbound from three workloads, and the thick intra-namespace mesh. Covered: hostPort (Task 2's node CIDRs + the collector analysis), cross-namespace inbound (Task 4 — and the census reduced "three workloads" to two, monitor-bridge and homelab-mcp, because traefik is the third and is handled in the baseline), intra-namespace mesh (Task 2's `podSelector: {}`). The spec's explicit slice-3 warning about prometheus rescheduling is answered by `netpol_baseline_obs_node_cidrs`.

**Type consistency.** `netpol_baseline_obs_enforced` and `netpol_baseline_obs_node_cidrs` are used in Tasks 2, 5 and 6 under exactly those names. `_labelled_workloads()` returns `(role, name)` tuples in Task 1 and is consumed in that shape in Task 3.

**Known gap, deliberate.** The hostPort DNAT source address remains inferred rather than measured — no packet capture is available without `sudo`, which is denied. Task 6 Step 5 converts it into an observed outcome instead. If the collector is the one thing that breaks, that inference is the cause.
