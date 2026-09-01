# NetworkPolicy slice 5 — flip the baseline to namespace scope

**Spec:** `docs/networkpolicy-default-deny.md`

**Goal:** switch `netpol_baseline_scope` from `label` to `namespace`, so the baseline fences every
pod in `homelab` — including pods nobody labelled and pods that do not exist yet.

**Deployed 2026-08-20.** 81 pods fenced, 4 exempt, both namespaces on the expression selector. All
eight probes passed, no probe needed changing, and no route 5xx'd. Findings are in
`docs/networkpolicy-default-deny.md`.

**Architecture:** the flip is one variable read by one Jinja conditional in one template, applied
atomically by a single `kubectl apply`. The work is not the flip; it is the exemption the flip
needs and the gate that proves the exemption is correct.

---

## Global Constraints

- **Ingress only.** kube-router enforces ingress and silently ignores egress on this cluster.
- **Policies are additive.** Selecting a pod with a second policy can only widen it, never narrow it.
- **Rollback is a variable, never a deletion.** `kubectl delete` is denied and `kubectl apply` does
  not prune. Rollback is `netpol_baseline_scope: label` plus a redeploy.
- **A `ports:` entry matches the pod's port, not the Service's.**
- **`ingress: []` denies all; `ingress: [{}]` allows all.** The likeliest catastrophic typo.
- **Deploy the labels first, verify them live, then the policies.** One combined run is not atomic.

---

## What the flip actually changes

Every pod in `homelab` that carries no `netpol-baseline: enforced` label today. Enumerated live on
2026-08-20, after slice 4.5's last label landed:

| Pod | Category | Effect of the flip |
|---|---|---|
| `flaresolverr`, `headlamp`, `n8n`, `registry` | Own a **tighter** bespoke policy | **Widened** — this is the problem to solve |
| `build-*` (7 buildkit Jobs) | Transient | Fenced. Harmless: `buildctl-daemonless.sh` starts buildkitd inside the pod, so the build's only client is loopback |
| `netpol-baseline-probe-*` (5) | Transient | Fenced. Harmless: every probe leg is **outbound**, and an ingress policy governs what reaches a pod, not what it reaches |
| `pi-peer-backup`, `seed-n8n-data` | Transient | Fenced. Both are outbound-only |
| `ctx-probe` | Ownerless bare pod, 5+ days old | Fenced. Harmless, and named here so a future reader does not mistake it for a regression |

**No Service-backed workload changes behaviour.** The four that would change are exempted below;
everything else is either already labelled or has no inbound caller. The flip's real effect is on
pods added *after* it — which is the point of the slice.

## Two spec bullets that no longer hold

The spec's slice-5 section predicts two consequences. One is stale and one is exactly right.

**Stale — "`podSelector: {}` fences each probe's control target."** The spec expects traefik's own
policy to admit only `app: traefik`, observability's prometheus and the two cni0 `/32`s, so every
probe dialing `traefik:80` for its control would fail. Read live on 2026-08-20, traefik's policy is:

```json
[{"ports":[{"port":8000},{"port":8443}]},
 {"from":[{"namespaceSelector":…observability, "podSelector":…prometheus}],"ports":[{"port":8080}]}]
```

The first rule has **no `from:`** — it is open to every source. Slice 4 gave traefik that open-port
rule after the spec paragraph was written. So every control leg survives untouched:

| Probe | Control leg | Survives because |
|---|---|---|
| slice 1, slice 2 | `traefik:80` **and** `speedtest:80` | open-port rule; sentinel policy admits `netpol-probe: control` |
| slice 3, slice 4, headlamp | `traefik:80` | open-port rule |
| slice 4.5 | `speedtest:80` | sentinel policy |
| prowlarr | `prowlarr:9696` | prowlarr's policy already admits `flaresolverr-netpol-probe` |
| registry | DNS resolution only | no connection to admit |

No probe needs changing. The spec bullet gets corrected rather than implemented.

**Exactly right — "the `flaresolverr` exemption's rationale expires."** Under `podSelector: {}` the
baseline selects flaresolverr regardless of its guard exemption, producing the widening the
exemption existed to avoid. The same applies to headlamp, n8n and registry. This is the slice's
real work.

## The exemption

Selector under namespace scope becomes an opt-**out**:

```yaml
  podSelector:
    matchExpressions:
      - key: netpol-baseline-exempt
        operator: DoesNotExist
```

`DoesNotExist` and not `NotIn`: a pod without the key satisfies `DoesNotExist` unambiguously, where
`NotIn` semantics for an absent key are the kind of thing that reads two ways.

Four workloads gain `netpol-baseline-exempt: "true"` on their pod template. The existing
`netpol-baseline: enforced` labels stay: they select nothing under namespace scope, but they are the
rollback path back to `label` scope, and removing them would roll 40 pods for no gain.

**The hazard inverts.** Slice 4.5 failed because a partial run left policies applied with labels
missing, and every fenced UI returned 5xx — loud. Here a partial run leaves the four **widened**
instead, which is silent. So the exempt labels land first and get verified live before the flip.

## The gate

Slice 4.5 proved a template-parsing guard is not enough: `n8n-runners` sat unlabelled in the cluster
for ~16 hours while `test_netpol_baseline_labels.py` stayed green, because the guard reads templates
and the cluster had drifted. The spec's gate ("enumerate pods lacking the label, fail if non-empty")
also inverts under an opt-out — unlabelled is now the *fenced* case, which is correct, not a fault.

The gate becomes, read from the live cluster inside `netpol-baseline`'s tasks, before the apply:

> The set of pods carrying `netpol-baseline-exempt` must equal `netpol_baseline_exempt_workloads`
> exactly. A missing entry means a widening about to happen; an extra entry means a pod fenced by
> nothing.

**What catches a kube-router selector bug.** `matchExpressions` is standard Kubernetes, but the gate
reads it through the API server, which cannot prove kube-router agrees. The existing probes do: if
kube-router mishandled the expression the baseline would select nothing, and every inverted leg
across five probes would then reach its target and fail the run.

## Observability gets the same flip

`observability` has 7 pods, all 7 labelled. Its baseline already admits `podSelector: {}` — the
whole namespace — plus traefik and four node CIDRs, so the flip changes nothing for a pod that
exists and closes the same "new pod is unfenced by default" gap for one that does not. It carries
its own flag, so it gets its own scope variable rather than sharing `homelab`'s.

---

## Tasks

### Task 1 — Exempt the four bespoke workloads

**Files** — note flaresolverr has no role of its own; it is deployed by `prowlarr`:

- `ansible/roles/k8s/prowlarr/templates/deployment-flaresolverr.yaml.j2`
- `ansible/roles/k8s/headlamp/templates/deployment.yaml.j2`
- `ansible/roles/k8s/n8n/templates/deployment.yaml.j2`
- `ansible/roles/k8s/registry/templates/deployment.yaml.j2`
- `ansible/tests/test_netpol_baseline_labels.py`

Add `netpol-baseline-exempt: "true"` beside each pod template's existing `app:` label, with a
one-line comment naming the bespoke policy it protects. Extend the guard with two assertions: each
of the four roles carries the exempt label, and no other role does.

**The n8n trap.** `deployment-runners.yaml.j2` sits beside `deployment.yaml.j2` in the same role,
and the exempt label must NOT land on it. The bespoke policy `n8n-broker` selects `app: n8n` only;
`n8n-runners` has no bespoke policy and is fenced by the baseline alone, so exempting it would
unfence it silently. The Task 3 gate catches a stray only because its allow-list is exactly four
app names.

### Task 2 — Render the opt-out selector

**Files:** `ansible/roles/k8s/netpol-baseline/{defaults/main.yml,templates/networkpolicy.yaml.j2,templates/networkpolicy-observability.yaml.j2}`.

Replace the `namespace` branch's bare `{}` with the `matchExpressions` form above, in both
templates. Add `netpol_baseline_exempt_workloads` (the four app names) and
`netpol_baseline_obs_scope: label` to defaults. Do **not** flip either scope yet — Task 2 ships the
mechanism, Task 5 pulls the lever.

### Task 3 — The live exempt-set gate

**Files:** `ansible/roles/k8s/netpol-baseline/tasks/main.yml`.

Before the manifest apply, query the live exempt set and assert it equals
`netpol_baseline_exempt_workloads`, failing with both sides printed. Runs only when
`netpol_baseline_scope == 'namespace'`, so it costs nothing until the flip.

### Task 4 — Docs

**Files:** `docs/networkpolicy-default-deny.md`.

Correct the stale control-target bullet (line ~130) with the live traefik policy that refutes it.
Correct "The probes in particular must stay unfenced to prove anything" (line ~691) — the spec
itself explains why an ingress policy selecting a probe pod cannot break an outbound assertion.
Add the slice 5 section.

### Task 5 — Deploy, in two verified stages

**Stage A** — deploy `prowlarr` (which carries flaresolverr), `headlamp`, `n8n`, `registry`. Then verify live, and do not
continue until it reads exactly four:

```
kubectl -n homelab get pods -L netpol-baseline-exempt
```

Deploying `prowlarr` also re-runs its own netpol probe, whose control leg dials `prowlarr:9696`.
Nothing has changed for that probe at stage A, so it must pass — and if it does not, the cause is
not the exemption.

**Stage B** — flip `netpol_baseline_scope: namespace` and `netpol_baseline_obs_scope: namespace`,
deploy `netpol-baseline`. The gate from Task 3 runs first and fails the deploy if stage A drifted.

### Task 6 — Verify

- The live selector is the expression form, in both namespaces.
- All five netpol-baseline probes pass in one run, plus the prowlarr, headlamp and registry probes.
- Functional spot checks across the fencing shapes: HA, Uptime Kuma, scrutiny, karakeep, n8n,
  headlamp, registry.
- Prometheus targets up, monitor-bridge all-OK.

**Rollback**, if any of it fails: `netpol_baseline_scope: label`, redeploy `netpol-baseline`. That
restores the label selector; every workload keeps its label, so nothing else has to move.
