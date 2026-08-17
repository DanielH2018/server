# Default-deny ingress — Slice 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fence the media stack and the two push bridges — ten workloads — behind the existing `baseline-ingress` policy, with explicit allow policies for the four that have real pod-to-pod callers.

**Architecture:** Slice 1 built the mechanism: a `baseline-ingress` NetworkPolicy selecting pods labelled `netpol-baseline: enforced`, admitting Traefik, the Prometheus pod in `observability`, and the two node bridges. This slice adds the label to ten more workloads and adds four per-workload policies whose allows union with the baseline's.

**Tech Stack:** k3s + kube-router netpol controller, Ansible (`roles/k8s/manifests`), Jinja2 manifests, alpine probe Jobs.

**Spec:** `docs/networkpolicy-default-deny.md` · **Prior slice:** `docs/networkpolicy-default-deny-plan.md`

## Scope

**In:** sonarr, radarr, prowlarr, qbittorrent (label + policy); bazarr, tdarr, configarr, janitorr, monitor-bridge, autofix-bridge (label only).

**Out — jellyfin, deliberately.** It is exposed on MetalLB VIP `10.0.0.241` with `externalTrafficPolicy: Local`, so TVs and phones arrive as `10.0.0.x` and an unqualified fence drops all of them. Allowing `lan_subnet` would fix that, but slice 1 measured that **pod → VIP is SNAT'd to the node bridge, which the baseline already admits** — so any pod reaches jellyfin through its VIP regardless, and fencing only its ClusterIP would overstate what was achieved. mosquitto and Pi-hole share the identical limitation, so all three move to slice 4 where it is documented once.

## Global Constraints

- **Ingress only.** Egress policies select pods and block nothing on this cluster. Never add `policyTypes: [Egress]`.
- **Namespace:** `{{ k8s_namespace }}` — never hardcode `homelab`.
- **Every per-workload policy must name `app: traefik` itself.** See the hazard below. Do not "optimise" it away on the grounds that the baseline already admits traefik.
- **`ingress: []`** (empty list) denies everything; **`ingress: [{}]`** (one empty rule) allows everything.
- **`from` peers:** separate list items OR; sibling keys within one item AND. A bare `podSelector` is scoped to the policy's own namespace.
- Labels go on `spec.template.metadata.labels` — never `spec.selector.matchLabels` (immutable; apply fails), never the Deployment's own `metadata.labels` (no effect).
- Deploy with `./scripts/deploy.sh --tags "<name>"`. Exit **75** means the lock was busy and nothing deployed.
- Commits are signed. Never `--no-verify` / `--no-gpg-sign`.

### The rollout hazard that shapes every policy here

A per-workload policy selects `app: sonarr`. During a rollout the **old** pod also carries `app: sonarr` but does **not** yet carry `netpol-baseline: enforced` — so the baseline does not select it, and the only policy selecting it is the new per-workload one. If that policy omits Traefik, the old pod is fenced away from Traefik for the whole rollout and the UI 502s until the new pod is Ready.

This is why each policy below lists Traefik explicitly. It is deliberate redundancy with the baseline, not an oversight.

### The host-origin path, and why it is not pre-allowed

`ansible/roles/k8s/sonarr/tasks/verify.yml` does a `uri:` from the Ansible controller (on daniel-box) to sonarr's **ClusterIP** on every sonarr deploy, and the fake-remux host crons do the same on a timer. Whether that traffic arrives as the node's `cni0` address (`10.42.0.1`, already admitted by the baseline) or as the node IP (`10.0.0.215`, not admitted) is **unverified** — no app in this stack logs client IPs, so it could not be settled by observation beforehand.

**This plan does not pre-allow the node IPs.** sonarr's own `verify.yml` runs on every deploy and fails the play loudly if the path is blocked, which makes the deploy itself the experiment. Task 5 records the answer either way. Adding both address forms up front would have made the deploy pass without revealing which entry did the work, and would have left an unexamined allowance in the policy permanently.

### Rollback for the four per-workload policies: the var flip is complete

`netpol_baseline_enforced: false` **does** roll sonarr, radarr, prowlarr and qbittorrent back, in full — the same one-line flip that rolls back slice 1.

It works because the baseline's off-state is an *allow-all rule*, not an absent policy. With the flag off, `netpol-baseline/templates/networkpolicy.yaml.j2` renders `ingress:` as `- {}` — one empty rule, which admits everything — while its `podSelector` (`netpol-baseline: enforced`) is unchanged and still selects all four workloads. NetworkPolicies are **additive**: a pod admits the union of every policy that selects it, so a permissive baseline readmits everything to those four regardless of what their own policies list. Their allow lists do not narrow anything once the baseline is wide.

**Do not hand-patch the four live NetworkPolicy objects instead.** `ingress: []` and `ingress: [{}]` are exact inverses, and the spec names that pair as the easiest thing in this design to get backwards — patching in the wrong one turns a partial outage into a total deny of the workload you were trying to unblock.

**The one residual the flag does not cover:** a pod carrying `app: <name>` but *not* `netpol-baseline: enforced` — i.e. an old pod part-way through a rollout — is selected only by its per-workload policy, which the flag does not touch. That window closes at the first rollout after this branch, because every pod created from then on carries both labels.

---

### Task 1: Per-workload policies for the four with pod callers

**Files:**
- Create: `ansible/roles/k8s/sonarr/templates/networkpolicy.yaml.j2`
- Create: `ansible/roles/k8s/radarr/templates/networkpolicy.yaml.j2`
- Create: `ansible/roles/k8s/prowlarr/templates/networkpolicy-prowlarr.yaml.j2`
- Create: `ansible/roles/k8s/qbittorrent/templates/networkpolicy.yaml.j2`
- Modify: each of those four roles' `tasks/main.yml` to add the new file to `manifests_files`

**Interfaces:**
- Consumes: the `netpol-baseline: enforced` label and `baseline-ingress` policy from slice 1 (already merged and live).
- Produces: NetworkPolicies named `sonarr`, `radarr`, `prowlarr`, `qbittorrent` in `{{ k8s_namespace }}`.

**Note on prowlarr's filename:** that role already has `networkpolicy.yaml.j2`, which fences **flaresolverr** (not prowlarr). Do not edit or replace it. The new file is `networkpolicy-prowlarr.yaml.j2` and must be added to `manifests_files` alongside the existing entry.

- [ ] **Step 1: Write sonarr's policy**

`ansible/roles/k8s/sonarr/templates/networkpolicy.yaml.j2`:

```jinja
---
# Slice 2 of the ingress default-deny. The baseline (roles/k8s/netpol-baseline) admits traefik,
# prometheus and the node bridges to every pod carrying `netpol-baseline: enforced`. This file adds
# sonarr's own callers, which are all direct pod → ClusterIP and therefore invisible to the
# baseline.
#
# TRAEFIK IS LISTED HERE DELIBERATELY, even though the baseline also admits it. This policy selects
# `app: sonarr`, which the OLD pod carries during a rollout — and that pod does not yet carry the
# baseline label, so the baseline does not select it. Without this rule the sonarr UI 502s for the
# length of every rollout. Do not remove it as redundant.
#
# bazarr addresses sonarr by BARE NAME from its own application database, not from any manifest
# (roles/k8s/bazarr/defaults/main.yml). It is in this list because of that comment, not because a
# grep found it — dropping it would break subtitle fetching silently.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: sonarr
  namespace: {{ k8s_namespace }}
spec:
  podSelector:
    matchLabels:
      app: sonarr
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: traefik
        - podSelector:
            matchExpressions:
              - key: app
                operator: In
                values:
                  - janitorr        # templates/config/application.yml.j2 — deploy-time verify too
                  - configarr       # CronJob; its pod template carries app: configarr
                  - monitor-bridge  # env-secret.yaml.j2, cluster FQDN
                  - autofix-bridge  # env-secret.yaml.j2, cluster FQDN
                  - bazarr          # app DB, bare name — see header
      ports:
        - protocol: TCP
          port: {{ container_item.port }}
```

- [ ] **Step 2: Write radarr's policy**

`ansible/roles/k8s/radarr/templates/networkpolicy.yaml.j2` — identical in shape to sonarr's, with `app: radarr` in the `podSelector`, `name: radarr`, and the same five callers. Repeat the full header comment (adjusted to radarr); do not write "same as sonarr" — these files are read one at a time.

```jinja
---
# Slice 2 of the ingress default-deny. The baseline admits traefik, prometheus and the node bridges
# to every pod carrying `netpol-baseline: enforced`. This file adds radarr's own callers, which are
# all direct pod → ClusterIP and therefore invisible to the baseline.
#
# TRAEFIK IS LISTED HERE DELIBERATELY, even though the baseline also admits it. This policy selects
# `app: radarr`, which the OLD pod carries during a rollout — and that pod does not yet carry the
# baseline label. Without this rule the radarr UI 502s for the length of every rollout.
#
# bazarr addresses radarr by BARE NAME from its own application database, not from any manifest.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: radarr
  namespace: {{ k8s_namespace }}
spec:
  podSelector:
    matchLabels:
      app: radarr
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: traefik
        - podSelector:
            matchExpressions:
              - key: app
                operator: In
                values:
                  - janitorr
                  - configarr
                  - monitor-bridge
                  - autofix-bridge
                  - bazarr
      ports:
        - protocol: TCP
          port: {{ container_item.port }}
```

- [ ] **Step 3: Write prowlarr's policy**

`ansible/roles/k8s/prowlarr/templates/networkpolicy-prowlarr.yaml.j2`:

```jinja
---
# Slice 2 of the ingress default-deny. NOTE: this role's OTHER policy file, networkpolicy.yaml.j2,
# fences flaresolverr and is unrelated to this one. Both are applied.
#
# TRAEFIK IS LISTED HERE DELIBERATELY — see the rollout hazard in the slice-2 plan: this policy
# selects `app: prowlarr`, which the pre-rollout pod carries without the baseline label.
#
# sonarr and radarr reach prowlarr by bare name from their own application databases — the indexer
# definitions live on their PVCs, not in this repo. Recorded in
# roles/k8s/sonarr/templates/isolation-probe-job.yaml.j2.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: prowlarr
  namespace: {{ k8s_namespace }}
spec:
  podSelector:
    matchLabels:
      app: prowlarr
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: traefik
        - podSelector:
            matchExpressions:
              - key: app
                operator: In
                values:
                  - monitor-bridge
                  - sonarr
                  - radarr
      ports:
        - protocol: TCP
          port: {{ container_item.port }}
```

- [ ] **Step 4: Write qbittorrent's policy**

`ansible/roles/k8s/qbittorrent/templates/networkpolicy.yaml.j2`:

```jinja
---
# Slice 2 of the ingress default-deny.
#
# sonarr and radarr reach this pod as the download client at `wireguard:8080` — a compat Service
# (service-wireguard.yaml.j2) that selects `app: qbittorrent`, carrying the hostname their SQLite
# databases were seeded with under Docker. The Service name differs; the pod is the same, so a
# podSelector on `app: qbittorrent` covers it. The PORT is the WebUI port, not container_item.port.
#
# TRAEFIK IS LISTED HERE DELIBERATELY — see the rollout hazard in the slice-2 plan.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: qbittorrent
  namespace: {{ k8s_namespace }}
spec:
  podSelector:
    matchLabels:
      app: qbittorrent
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: traefik
        - podSelector:
            matchExpressions:
              - key: app
                operator: In
                values:
                  - sonarr
                  - radarr
      ports:
        - protocol: TCP
          port: {{ qbittorrent_k8s_webui_port }}
```

- [ ] **Step 5: Wire each policy into its role**

In each of the four roles' `tasks/main.yml`, add the new manifest to `manifests_files`. Put it **first** in the list, matching the convention in `roles/k8s/headlamp/tasks/main.yml` (policy before the workload, so it exists before the pod can be scheduled). For prowlarr, add `networkpolicy-prowlarr.yaml` alongside the existing `networkpolicy.yaml` — do not replace it.

- [ ] **Step 6: Verify all four render**

Run: `uv run python scripts/validate_k8s_manifests.py`
Expected: PASS, template count up by 4.

Run: `uv run ansible-lint ansible/roles/k8s/sonarr/ ansible/roles/k8s/radarr/ ansible/roles/k8s/prowlarr/ ansible/roles/k8s/qbittorrent/`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add ansible/roles/k8s/sonarr ansible/roles/k8s/radarr ansible/roles/k8s/prowlarr ansible/roles/k8s/qbittorrent
git commit -m "Add slice-2 ingress policies for the four arr services with pod callers

Each names traefik itself rather than relying on the baseline. A
per-workload policy selects app: <name>, which the OLD pod carries
during a rollout -- and that pod has no baseline label yet, so the
baseline does not select it. Without the explicit rule the UI 502s for
the length of every rollout.

bazarr -> sonarr/radarr and sonarr/radarr -> prowlarr and qbittorrent
are configured in the applications' own databases, not in any manifest.
They are in these lists on the strength of the repo's own recorded
comments, not because a grep found them.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Label the ten workloads

**Files:** Modify the pod template in each of:
- `ansible/roles/k8s/{sonarr,radarr,prowlarr,qbittorrent,bazarr,tdarr,janitorr,monitor-bridge,autofix-bridge}/templates/deployment.yaml.j2`
- `ansible/roles/k8s/configarr/templates/cronjob.yaml.j2` — **the pod template inside `jobTemplate`**, not the CronJob's own metadata

**Interfaces:** Consumes the `netpol-baseline: enforced` label defined by slice 1.

- [ ] **Step 1: Add the label to each pod template**

Add `netpol-baseline: enforced` under `spec.template.metadata.labels`, as a sibling of `app: <name>`:

```yaml
spec:
  template:
    metadata:
      labels:
        app: sonarr
        netpol-baseline: enforced
```

For configarr the path is `spec.jobTemplate.spec.template.metadata.labels`. Verify that specific nesting rather than pattern-matching the Deployments — a CronJob has two `template:` levels and only the inner one creates pods.

**Critical:** never `spec.selector.matchLabels` (immutable field, apply fails) and never the Deployment's own top-level `metadata.labels` (NetworkPolicy matches pod labels, so it has no effect).

- [ ] **Step 2: Extend the label guard test**

Modify `ansible/tests/test_netpol_baseline_labels.py` — it currently pins the labelled set to slice 1's six. Add slice 2's ten so the set is sixteen. The test must still fail on both a dropped and an added label.

Read the existing file first; keep its structure and naming. If it hardcodes a `SLICE_1` constant, add a `SLICE_2` constant and assert against the union rather than renaming the existing one.

**The test almost certainly globs `deployment.yaml.j2`, and configarr has no Deployment** — its pod template is in `cronjob.yaml.j2`. Widen the glob to all of `templates/*.j2` (or add the CronJob explicitly) and re-verify the count, or configarr will read as "label missing" forever and the test will fail for a reason that has nothing to do with a real regression. Confirm by running it before and after adding configarr to the expected set.

- [ ] **Step 3: Verify**

Run: `uv run python scripts/validate_k8s_manifests.py`
Expected: PASS.

Run: `uv run pytest ansible/tests/test_netpol_baseline_labels.py -v`
Expected: PASS.

Run: `grep -rln "netpol-baseline: enforced" ansible/roles/k8s/*/templates/`
Expected: exactly 16 files.

Run: `grep -rn "netpol-baseline" ansible/roles/k8s/*/templates/ | grep -c matchLabels`
Expected: `0`.

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/k8s ansible/tests/test_netpol_baseline_labels.py
git commit -m "Opt the media stack and both bridges into the ingress baseline

Ten workloads. Six of them -- bazarr, tdarr, configarr, janitorr,
monitor-bridge, autofix-bridge -- need no policy of their own: the last
four have no Service at all, and the first two are reached only through
traefik.

configarr's label goes on the pod template inside jobTemplate. A CronJob
has two template levels and only the inner one creates pods, so the
outer one would have looked right and matched nothing.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: The slice-2 probe

Slice 1's probe proves the baseline fences a workload with no in-cluster caller. Slice 2's risk is different and needs its own assertions: these workloads have *allowed* callers, so the probe must prove both that an unlisted pod is blocked **and** that a listed caller still gets through.

**Files:**
- Create: `ansible/roles/k8s/netpol-baseline/templates/netpol-probe-slice2-job.yaml.j2`
- Modify: `ansible/roles/k8s/netpol-baseline/tasks/main.yml`

- [ ] **Step 1: Write the probe**

Model it on `netpol-baseline/templates/netpol-probe-job.yaml.j2` — read that file first and match its structure, `securityContext`, resources and comment style.

Two pods' worth of assertions cannot run in one Job, so this Job's pod carries **no** `app` label that any policy admits, and asserts only the negative direction plus controls:

1. **Control** — `nc -w 5 -z traefik 80` must succeed. Failure means the probe proves nothing.
2. **Negative control** — an unfenced pod is still reachable. Use `homepage 3000`.
3. **Assertion A** — `nc -w 5 -z sonarr {{ port }}` must FAIL. This pod is not one of sonarr's five allowed callers.
4. **Assertion B** — `nc -w 5 -z qbittorrent {{ qbittorrent_k8s_webui_port }}` must FAIL.

Inverted assertions: a successful connection is the failure branch, `exit 1`, with a message naming the likely cause (enforcement off, or the label missing from the pod template).

**The positive direction is not probed here, deliberately, because it is already proven by two things that run anyway:** `roles/k8s/janitorr/tasks/verify.yml` opens TCP connections to `sonarr:8989`, `radarr:7878` and `jellyfin:8096` from inside the janitorr pod and fails the deploy if blocked; and `roles/k8s/sonarr/tasks/verify.yml` calls sonarr's API from the host. State this in the probe's header so nobody adds a redundant positive probe later.

The container's `command` — the part that is genuinely new; the surrounding Job YAML is slice 1's, unchanged apart from the name and the manifest directory:

```sh
if ! nc -w 5 -z traefik 80; then
  echo "CONTROL FAILED: cannot reach traefik:80, so nothing below is attributable"
  echo "to the policy. Check DNS and the traefik Service."
  exit 1
fi
echo "control ok: traefik:80 reachable"

if ! nc -w 5 -z homepage 3000; then
  echo "NEGATIVE CONTROL FAILED: cannot reach the unfenced homepage:3000 either."
  echo "This pod cannot open connections at all, so the assertions below would pass"
  echo "whether or not the policies work."
  exit 1
fi
echo "negative control ok: unfenced homepage:3000 reachable"

# This pod carries no app label that sonarr's policy admits. Its five allowed callers are
# janitorr, configarr, monitor-bridge, autofix-bridge and bazarr.
if nc -w 5 -z sonarr {{ hostvars[inventory_hostname]['containers_list'] | selectattr('name','equalto','sonarr') | map(attribute='port') | first }}; then
  echo "POLICY NOT ENFORCED: reached sonarr directly from a pod that is not one of its"
  echo "five allowed callers. Either netpol_baseline_enforced is false, or the label is"
  echo "missing from sonarr's POD TEMPLATE, or sonarr's policy admits too much."
  exit 1
fi
echo "isolated: sonarr unreachable from an unlisted caller"

if nc -w 5 -z qbittorrent {{ qbittorrent_k8s_webui_port }}; then
  echo "POLICY NOT ENFORCED: reached qbittorrent from a pod that is not sonarr or radarr."
  exit 1
fi
echo "isolated: qbittorrent unreachable from an unlisted caller"
```

If that `containers_list` lookup for sonarr's port is awkward in this role's context, hardcoding `8989` with a comment naming `host_vars/daniel-box.yml` as the source is acceptable — the probe is a test, and a wrong port here fails loudly rather than silently.

- [ ] **Step 2: Wire it into the role**

Add render + clear + run + wait + report + show + fail tasks mirroring the slice-1 probe's, into their own manifest directory `/etc/rancher/k3s/manifests/netpol-baseline-probe-slice2`. Gate the run/wait/report/show/fail tasks on the same two conditions the slice-1 probe uses:

```yaml
  when:
    - netpol_baseline_enforced | bool
    - not k8s_no_mutate
```

Leave the render task ungated, as slice 1's is.

- [ ] **Step 3: Verify**

Run: `uv run python scripts/validate_k8s_manifests.py && uv run ansible-lint ansible/roles/k8s/netpol-baseline/`
Expected: both PASS.

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/k8s/netpol-baseline
git commit -m "Probe slice 2's fencing from an unlisted caller

Slice 1's probe proves a workload with no in-cluster caller is
unreachable. Slice 2's workloads have allowed callers, so the question
is different: does the policy admit exactly those and no one else.

The positive direction needs no new probe. janitorr's verify opens TCP
to sonarr, radarr and jellyfin from inside its own pod, and sonarr's
verify calls its API from the host -- both run on every deploy and both
fail the play if the fencing is wrong.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Deploy

Two stages, for the reason slice 1 established: labels must reach the pods before enforcement can select them, and separating the two makes a failure attributable.

Unlike slice 1, the baseline is **already enforcing**, so labelling a workload fences it immediately. The per-workload policies must therefore land in the same deploy as the labels — which they do, since both live in each service's own role.

- [ ] **Step 1: Confirm the ten are healthy first**

Run: `kubectl -n homelab get pods -l 'app in (sonarr,radarr,prowlarr,qbittorrent,bazarr,tdarr,janitorr,monitor-bridge,autofix-bridge)'`
Expected: all `1/1 Running`. Fix or drop any that are not — a probe failure against an already-sick workload is unattributable.

- [ ] **Step 2: Deploy the four with policies first, one at a time**

Deploy in dependency order so a break is isolated. `prowlarr` first (its callers sonarr/radarr are not yet fenced, so nothing depends on prowlarr's own fencing):

Run: `./scripts/deploy.sh --tags "prowlarr"`
Then check: `kubectl -n homelab get pods -l app=prowlarr` → `1/1 Running`, and load the prowlarr UI through Traefik.

Then: `./scripts/deploy.sh --tags "qbittorrent"`, then `"sonarr"`, then `"radarr"`.

**Watch sonarr's deploy specifically.** `roles/k8s/sonarr/tasks/verify.yml` calls sonarr's ClusterIP from the host. If it fails with a connection error, the host-origin path is NOT covered by the baseline's `cni0` ipBlocks — that is the open question this plan deliberately did not pre-answer. Record it, then add the node IPs (`10.0.0.215`, `10.0.0.161`) to sonarr's and radarr's policies as `ipBlock` entries with a comment explaining they are the host path.

- [ ] **Step 3: Deploy the six label-only workloads**

Run: `./scripts/deploy.sh --tags "bazarr,tdarr,configarr,janitorr,monitor-bridge,autofix-bridge"`

**Watch janitorr's deploy.** `roles/k8s/janitorr/tasks/verify.yml` opens TCP to `sonarr:8989`, `radarr:7878` and `jellyfin:8096` from inside the janitorr pod. This is the positive-path test for Task 1's policies; if sonarr's or radarr's allow list is wrong, it fails here.

- [ ] **Step 4: Deploy the probe**

Run: `./scripts/deploy.sh --tags "netpol-baseline"`
Expected: both probes print their assertions green.

- [ ] **Step 5: Verify the live state**

Run: `kubectl -n homelab get pods -l netpol-baseline=enforced --no-headers | wc -l`
Expected: **15** standing pods.

The durable invariant is **16 roles** — slice 1's six plus slice 2's ten — pinned by `ansible/tests/test_netpol_baseline_labels.py`. The pod count is not that number and never settles on it: configarr is a CronJob with no standing pod, so it contributes zero between runs. A count above 15 means a Job pod is still around and has not yet aged out — a configarr CronJob run, or the `configarr-deploy-*` one-off from this deploy. Count roles (or run the test); do not treat a pod count as the check.

Run: `kubectl -n homelab get networkpolicy`
Expected: `baseline-ingress`, `flaresolverr`, `headlamp`, `n8n-broker`, `registry`, plus the four new ones.

- [ ] **Step 6: Confirm the media stack still works end to end**

- Load sonarr, radarr, prowlarr, bazarr, tdarr and qbittorrent through Traefik.
- In sonarr, confirm the indexers still test green (proves sonarr → prowlarr).
- In sonarr, confirm the download client tests green (proves sonarr → qbittorrent via the `wireguard` Service).
- Run `uv run python scripts/probe.py targets` and confirm no new DOWN targets.
- Check monitor-bridge is still reporting: `uv run python scripts/probe.py alerts --days 1`.

- [ ] **Step 7: Commit any policy corrections the deploy forced**

If step 2 or 3 required adding the node ipBlocks, commit that separately with a message recording what was observed, not just what changed.

---

### Task 5: Record what the deploy settled

**Files:** Modify `docs/networkpolicy-default-deny.md`

- [ ] **Step 1: Record the host-origin answer**

Add to the spec whether host → ClusterIP arrives as `cni0` or the node IP, with the evidence (sonarr's verify passing or failing). This is the last unmeasured address form in the design, and slices 3–5 all depend on it.

- [ ] **Step 2: Update the slice table**

Mark slice 2 done. Move jellyfin explicitly into slice 4's row with the VIP reasoning, so the deferral is recorded where the next author reads it rather than only in this plan.

- [ ] **Step 3: Commit**

---

## Done when

- All 16 roles stamp `netpol-baseline: enforced` onto their pod template — asserted by `ansible/tests/test_netpol_baseline_labels.py`, not by a live pod count (15 pods stand; configarr's CronJob has none between runs).
- Four new NetworkPolicies are live.
- Both probes green.
- sonarr's indexers and download client both test green in its UI.
- janitorr's and sonarr's deploy-time verifies pass — those are the positive-path proof.
- The spec records the host-origin answer.
