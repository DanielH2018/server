# Default-deny ingress — Slice 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fence six traefik-only leaf apps behind a namespace baseline NetworkPolicy, proving the whole mechanism — policy, opt-in label, var-flip rollback, deploy-time probe — at near-zero blast radius.

**Architecture:** One `baseline-ingress` NetworkPolicy per namespace, owned by a new `k8s/netpol-baseline` role. Its `podSelector` starts as an **opt-in label** (`netpol-baseline: enforced`) so each slice fences only the workloads it labels; a final slice migrates the selector to `podSelector: {}`. Ingress-only, because egress is unenforced on this CNI.

**Tech Stack:** k3s + kube-router netpol controller, Ansible (`roles/k8s/manifests` render→apply→wait), Jinja2 manifests, alpine probe Jobs.

**Spec:** `docs/networkpolicy-default-deny.md`

## Scope

**This plan covers slice 1 only.** Slices 2–4 are deliberately not planned yet: both of the spec's open items are answered *by* slice 1 (whether `registry`'s `10.42.1.0/32` is load-bearing, and the observed source IP for pod→MetalLB-VIP traffic under ETP=Local), and slice 2's policies depend on those answers. One-line pointers:

- **Slice 2** — media stack + bridges. **Slice 3** — observability namespace. **Slice 4** — infra tier. **Slice 5** (new, see below) — migrate `podSelector` to `{}`.

## Global Constraints

- **Ingress only.** Egress policies select pods and block nothing on this cluster (measured 2026-08-07). Never add `policyTypes: [Egress]`.
- **Namespace:** `{{ k8s_namespace }}` — never hardcode `homelab`.
- **Node bridge addresses are `/32` host addresses:** `10.42.0.1/32`, `10.42.1.1/32`. **Never** use `10.42.0.0/16` — both pod CIDRs sit inside it and `ipBlock` matches on source IP regardless of whether the source is a pod, so that one line readmits every pod in the cluster.
- **The two ingress shapes are exact inverses:** `ingress: []` (empty list) **denies everything**; `ingress: [{}]` (one empty rule) **allows everything**.
- **`from` peers:** separate list items are OR'd; sibling keys *within one item* are AND'd. A bare `podSelector` is always scoped to the policy's own namespace.
- **Labels go on `spec.template.metadata.labels`**, never on `spec.selector.matchLabels` (immutable field — apply fails) and never on the Deployment's own `metadata.labels` (NetworkPolicy matches pod labels).
- **Rollback is a var flip, never a deletion.** `kubectl delete` is denied and `kubectl apply` does not prune, so removing a template leaves the live policy enforcing.
- Deploy with `./scripts/deploy.sh --tags "<name>"` (takes the git-tree lock). Exit 75 means the lock was busy and nothing deployed.

---

### Task 1: The `netpol-baseline` role

Creates the policy in its off (permissive) state first, so the role lands and applies without fencing anything. Task 3 flips it on.

**Files:**
- Create: `ansible/roles/k8s/netpol-baseline/defaults/main.yml`
- Create: `ansible/roles/k8s/netpol-baseline/templates/networkpolicy.yaml.j2`
- Create: `ansible/roles/k8s/netpol-baseline/tasks/main.yml`
- Modify: `ansible/inventory/host_vars/daniel-box.yml` (add to `containers_list`, immediately after the `traefik` entry at line 72)

**Interfaces:**
- Produces: the `netpol-baseline` role, variables `netpol_baseline_enforced` (bool), `netpol_baseline_scope` (`label`|`namespace`), `netpol_baseline_node_cidrs` (list), and the pod label `netpol-baseline: enforced` that Task 2 applies.

- [ ] **Step 1: Write the role defaults**

`ansible/roles/k8s/netpol-baseline/defaults/main.yml`:

```yaml
---
# Off by default in this first commit so the role can land and apply without fencing anything.
# Task 3 flips it to true in its own commit, which is also the rollback lever: set false and
# redeploy. NOT a deletion — `kubectl apply` does not prune, so removing this role's template
# would leave the live policy enforcing with nothing in the repo describing it.
netpol_baseline_enforced: false

# `label`     — the policy selects only pods carrying `netpol-baseline: enforced` (slices 1-4)
# `namespace` — the policy selects every pod via `podSelector: {}` (slice 5)
netpol_baseline_scope: label

# The node-side bridge addresses, for traffic that arrives with a node IP: containerd image
# pulls, hostPort paths, and the three host crons that dial traefik. Verified per node with
# `ip -4 -o addr show cni0`. These are /32 HOST addresses on purpose — see the plan's Global
# Constraints for why a /16 here would silently cancel the whole policy.
netpol_baseline_node_cidrs:
  - 10.42.0.1/32
  - 10.42.1.1/32
```

- [ ] **Step 2: Write the policy template**

`ansible/roles/k8s/netpol-baseline/templates/networkpolicy.yaml.j2`:

```jinja
---
# The namespace ingress baseline. Kubernetes admits every pod-to-pod connection by default and a
# NetworkPolicy constrains only the pods it SELECTS, so this is the object that makes every other
# policy in the repo mean something.
#
# INGRESS ONLY. An Egress policy selects pods correctly on this cluster and blocks nothing
# (measured 2026-08-07, recorded in sonarr/templates/isolation-probe-job.yaml.j2). Writing one
# would read like a control in the repo and do nothing in the cluster.
#
# Kubelet probe traffic needs no rule here: flaresolverr admits :8191 only from prowlarr yet is
# probed on that port and runs 1/1, and headlamp corroborates it.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: baseline-ingress
  namespace: {{ k8s_namespace }}
spec:
  podSelector:
{% if netpol_baseline_scope == 'namespace' %}
    {}
{% else %}
    matchLabels:
      netpol-baseline: enforced
{% endif %}
  policyTypes:
    - Ingress
  ingress:
{% if netpol_baseline_enforced %}
    # Three callers legitimately reach almost every pod. Stating them once here is the whole
    # design; the alternative is restating them in ~50 per-workload policies.
    - from:
        # Traefik is the front door for every web UI. Bare podSelector = this namespace only.
        - podSelector:
            matchLabels:
              app: traefik
        # Prometheus scrapes every workload. namespaceSelector and podSelector are SIBLING KEYS
        # in ONE list item, so they AND together: the prometheus pod in observability. As two
        # separate list items they would OR, admitting the entire observability namespace.
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: observability
          podSelector:
            matchLabels:
              app: prometheus
{% for cidr in netpol_baseline_node_cidrs %}
        - ipBlock:
            cidr: {{ cidr }}
{% endfor %}
{% else %}
    # OFF. One empty rule admits everything. `ingress: []` would be the exact opposite and deny
    # everything, which is the easiest thing in this file to get backwards.
    - {}
{% endif %}
```

- [ ] **Step 3: Write the role tasks**

`ansible/roles/k8s/netpol-baseline/tasks/main.yml`:

```yaml
---
# No Deployment, so manifests_rollout is empty — there is nothing to wait on. The policy takes
# effect on apply.
- name: Deploy the namespace ingress baseline
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_service: netpol-baseline
    manifests_rollout: ""
    manifests_files:
      - networkpolicy.yaml
```

- [ ] **Step 4: Register the role in `containers_list`**

In `ansible/inventory/host_vars/daniel-box.yml`, immediately after the `traefik` entry (line 72-74), insert:

```yaml
  # Immediately after traefik: the baseline references `app: traefik` and every workload below
  # is fenced by it. That play has no toposort and runs in list order.
  - name: netpol-baseline
    platform: k8s
```

- [ ] **Step 5: Verify it renders**

Run: `cd /home/ubuntu/server/.claude/worktrees/netpol-default-deny && uv run python scripts/validate_k8s_manifests.py`

Expected: PASS. The validator loads `defaults/main.yml` via `role_defaults()`, so with `netpol_baseline_enforced: false` it renders and YAML-parses the **off** branch.

- [ ] **Step 6: Verify the enforced branch also renders**

Temporarily set `netpol_baseline_enforced: true` in defaults, re-run the validator, confirm PASS, then set it back to `false`.

Run: `uv run python scripts/validate_k8s_manifests.py`
Expected: PASS both times. This exists because a `{% if %}` means the validator only ever structurally checks whichever branch the defaults select — the other half is unverified until something renders it.

- [ ] **Step 7: Commit**

```bash
git add ansible/roles/k8s/netpol-baseline ansible/inventory/host_vars/daniel-box.yml
git commit -m "Add a namespace ingress baseline role, off by default

Kubernetes admits every pod-to-pod connection by default and a
NetworkPolicy constrains only the pods it selects, so nothing in this
repo's four existing policies protects the other ~50 workloads.

This lands the policy object in its permissive state so the role and its
containers_list position can be verified without fencing anything. The
podSelector is an opt-in label rather than the whole namespace, so each
slice fences only what it labels.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Label the six leaf apps

The six workloads whose only in-cluster caller is Traefik. `terraria` and `valheim` are deliberately excluded — they have no `port` in `containers_list` and are reached over MetalLB LoadBalancer VIPs, not Traefik.

**Files:**
- Modify: `ansible/roles/k8s/bento-pdf/templates/deployment.yaml.j2`
- Modify: `ansible/roles/k8s/littlelink/templates/deployment.yaml.j2`
- Modify: `ansible/roles/k8s/speedtest/templates/deployment.yaml.j2`
- Modify: `ansible/roles/k8s/healthchecks/templates/deployment.yaml.j2`
- Modify: `ansible/roles/k8s/ical-proxy/templates/deployment.yaml.j2`
- Modify: `ansible/roles/k8s/code-server/templates/deployment.yaml.j2`

**Interfaces:**
- Consumes: the `netpol-baseline: enforced` pod label defined in Task 1.

- [ ] **Step 1: Add the label to each pod template**

In each of the six files, find the pod template's label block. It looks like this:

```yaml
spec:
  template:
    metadata:
      labels:
        app: bento-pdf
```

Add one line, so it becomes:

```yaml
spec:
  template:
    metadata:
      labels:
        app: bento-pdf
        netpol-baseline: enforced
```

**Critical:** this goes under `spec.template.metadata.labels` only. Do **not** add it to `spec.selector.matchLabels` (that field is immutable and the apply will fail) and do **not** add it to the Deployment's own top-level `metadata.labels` (NetworkPolicy matches pod labels, so it would have no effect).

- [ ] **Step 2: Verify all six render**

Run: `uv run python scripts/validate_k8s_manifests.py`
Expected: PASS.

- [ ] **Step 3: Confirm the label lands on exactly six pod templates and nowhere else**

Run: `grep -rn "netpol-baseline: enforced" ansible/roles/k8s/*/templates/deployment.yaml.j2`
Expected: exactly 6 lines — bento-pdf, littlelink, speedtest, healthchecks, ical-proxy, code-server.

Run: `grep -rn "netpol-baseline" ansible/roles/k8s/*/templates/deployment.yaml.j2 | grep -c "matchLabels"`
Expected: `0`.

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/k8s/bento-pdf ansible/roles/k8s/littlelink ansible/roles/k8s/speedtest \
        ansible/roles/k8s/healthchecks ansible/roles/k8s/ical-proxy ansible/roles/k8s/code-server
git commit -m "Opt six traefik-only leaf apps into the ingress baseline

These six have no in-cluster caller other than Traefik, so fencing them
is the smallest change that exercises the mechanism end to end. A
mistake here surfaces as a 502 on a page nobody depends on, rather than
as silence somewhere in the media stack.

terraria and valheim are excluded despite looking like leaves: they have
no port in containers_list and are reached over MetalLB VIPs, not
Traefik.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: The four-part probe job

The existing four probe jobs assert one thing: a non-caller cannot reach a restricted pod. That assertion is **insufficient here** — slice 1's apps have no in-cluster callers at all, so "the probe pod cannot reach the app" is equally true when the policy works, when the label was never applied, and when the app is simply down. This job asserts four things so that only real enforcement passes.

> **Correction applied during execution (commit `40d9a3ad`).** The code below names **bento-pdf** as the liveness and isolation target. That was wrong: bento-pdf has `use_authelia: true`, and Authelia has no bypass rule for `*.local.<domain>`, so the forwardauth middleware answers an unauthenticated request *before* Traefik proxies to the pod — the assertion would have measured the auth chain, not the app. A 401 would make the probe permanently red for an unrelated reason; a 302-to-portal would print "liveness ok" without ever contacting the app, restoring the exact false-pass this job exists to eliminate.
>
> Assertions 3 and 4 target **littlelink** instead (`use_authelia: false`, hostname `www`, port `3000`, and it carries the label from Task 2). The shipped file is the truth; treat the block below as the design rationale, not the current source. This is the same trap recorded in this repo as "an Authelia 302 doesn't prove the backend was reached" — cited in Task 4 step 9 below and then walked into here.

**Files:**
- Create: `ansible/roles/k8s/netpol-baseline/templates/netpol-probe-job.yaml.j2`
- Modify: `ansible/roles/k8s/netpol-baseline/defaults/main.yml` (add the probe image)
- Modify: `ansible/roles/k8s/netpol-baseline/tasks/main.yml` (render + run + report)

**Interfaces:**
- Consumes: `netpol_baseline_enforced` and the pod label from Tasks 1-2; the `domain` variable from `group_vars/all.yml`.
- Produces: Job `netpol-baseline-probe` in `{{ k8s_namespace }}`.

- [ ] **Step 1: Add the probe image to defaults**

Append to `ansible/roles/k8s/netpol-baseline/defaults/main.yml`:

```yaml
# Matches headlamp_k8s_netpol_probe_image and registry_k8s_netpol_probe_image.
netpol_baseline_probe_image: alpine:3.24
```

- [ ] **Step 2: Write the probe job**

`ansible/roles/k8s/netpol-baseline/templates/netpol-probe-job.yaml.j2`:

```jinja
---
# Proves the baseline is ENFORCED, not merely applied. Four assertions, because the obvious
# single one is not sufficient for this slice: these six apps have no in-cluster caller, so
# "cannot reach bento-pdf" is true under enforcement, under a missing label, AND when bento-pdf
# is down. Each assertion below eliminates one of those.
#
# This pod carries NO netpol-baseline label, so the policy does not select it. That is the point
# — it stands in for a compromised workload trying to move laterally.
apiVersion: batch/v1
kind: Job
metadata:
  name: netpol-baseline-probe
  namespace: {{ k8s_namespace }}
spec:
  activeDeadlineSeconds: 180
  backoffLimit: 0
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels:
        app: netpol-baseline-probe
    spec:
      restartPolicy: Never
      enableServiceLinks: false
      automountServiceAccountToken: false
      containers:
        - name: probe
          image: {{ netpol_baseline_probe_image }}
          command:
            - sh
            - -c
            - |
              # 1. CONTROL — the network and DNS work at all. traefik:80 is open to every source
              #    deliberately (see the spec: narrowing the front door buys nothing and would
              #    break every other probe job's control). A failure here means this probe proves
              #    nothing, rather than meaning the policy failed.
              if ! nc -w 5 -z traefik 80; then
                echo "CONTROL FAILED: cannot reach traefik:80, so nothing below is attributable"
                echo "to the policy. Check DNS and the traefik Service."
                exit 1
              fi
              echo "control ok: traefik:80 reachable"

              # 2. NEGATIVE CONTROL — an UNFENCED pod is still reachable from here. homepage
              #    carries no netpol-baseline label (it is slice 4), so if this fails the probe
              #    pod's own networking is broken and assertion 3 would pass for the wrong
              #    reason.
              if ! nc -w 5 -z homepage 3000; then
                echo "NEGATIVE CONTROL FAILED: cannot reach the unfenced homepage:3000 either."
                echo "This pod cannot open connections at all, so assertion 3 below would pass"
                echo "whether or not the policy works. Investigate before trusting this job."
                exit 1
              fi
              echo "negative control ok: unfenced homepage:3000 reachable"

              # 3. LIVENESS — bento-pdf is actually UP and Traefik can reach it. Goes through the
              #    Traefik VIP by hostname, which is the real allowed path, so this doubles as the
              #    positive test of the policy's traefik rule. If this fails but 4 passes, the app
              #    is down (or CrowdSec banned this source) and the fencing result is meaningless.
              if ! wget -q -T 10 --no-check-certificate -O /dev/null \
                   "https://bento-pdf.local.{{ domain }}/"; then
                echo "LIVENESS FAILED: bento-pdf is not reachable through traefik. Either the app"
                echo "is down or the baseline's traefik rule is wrong. Assertion 4 below cannot"
                echo "distinguish enforcement from an app that is simply not listening."
                exit 1
              fi
              echo "liveness ok: bento-pdf reachable through traefik"

              # 4. THE ASSERTION — direct pod-to-pod must FAIL. Inverted: success here is the
              #    failure case.
              if nc -w 5 -z bento-pdf 8080; then
                echo "POLICY NOT ENFORCED: reached bento-pdf:8080 directly from a pod that"
                echo "carries no netpol-baseline label. Either netpol_baseline_enforced is false,"
                echo "or the label is missing from bento-pdf's POD TEMPLATE (check"
                echo "spec.template.metadata.labels, not spec.selector.matchLabels)."
                exit 1
              fi
              echo "isolated: bento-pdf:8080 unreachable except through traefik"
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          resources:
            requests:
              cpu: 10m
              memory: 16Mi
            limits:
              cpu: 200m
              memory: 64Mi
```

- [ ] **Step 3: Wire the probe into the role**

Append to `ansible/roles/k8s/netpol-baseline/tasks/main.yml`:

```yaml
# ── prove the policy is enforced, rather than merely applied ────────────────────────────────
# Its own directory: `kubectl apply -f <dir>` reads every file in the directory it is given, and
# a Job's pod template is immutable, so a Job left beside the policy would be re-applied as
# either a no-op or an error.
- name: Create the netpol-baseline probe manifest directory
  tags: [config]
  ansible.builtin.file:
    path: /etc/rancher/k3s/manifests/netpol-baseline-probe
    state: directory
    mode: "0700"
    owner: root
    group: root
  become: true

- name: Render the netpol-baseline probe
  tags: [config]
  ansible.builtin.template:
    src: netpol-probe-job.yaml.j2
    dest: /etc/rancher/k3s/manifests/netpol-baseline-probe/netpol-probe-job.yaml
    mode: "0644"
    owner: root
    group: root
  become: true

- name: Clear the previous netpol-baseline probe run
  tags: [deploy]
  when: netpol_baseline_enforced
  ansible.builtin.command:
    cmd: >-
      k3s kubectl -n {{ k8s_namespace }} delete job netpol-baseline-probe
      --ignore-not-found --wait=true
  become: true
  changed_when: true

- name: Run the netpol-baseline probe
  tags: [deploy]
  when: netpol_baseline_enforced
  ansible.builtin.command:
    cmd: >-
      k3s kubectl apply -f
      /etc/rancher/k3s/manifests/netpol-baseline-probe/netpol-probe-job.yaml
  become: true
  changed_when: true

- name: Wait for the netpol-baseline probe
  tags: [deploy]
  when: netpol_baseline_enforced
  ansible.builtin.command:
    cmd: >-
      k3s kubectl -n {{ k8s_namespace }} wait --for=condition=complete
      job/netpol-baseline-probe --timeout=180s
  become: true
  changed_when: false
  register: netpol_baseline_probe
  failed_when: netpol_baseline_probe.rc != 0

- name: Report the netpol-baseline probe
  tags: [deploy]
  when: netpol_baseline_enforced
  ansible.builtin.command:
    cmd: "k3s kubectl -n {{ k8s_namespace }} logs job/netpol-baseline-probe"
  become: true
  changed_when: false
  register: netpol_baseline_probe_log

- name: Show the netpol-baseline probe result
  tags: [deploy]
  when: netpol_baseline_enforced
  ansible.builtin.debug:
    msg: "{{ netpol_baseline_probe_log.stdout_lines }}"
```

- [ ] **Step 4: Verify it renders and lints**

Run: `uv run python scripts/validate_k8s_manifests.py && uv run ansible-lint ansible/roles/k8s/netpol-baseline/`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/k8s/netpol-baseline
git commit -m "Prove the baseline with a four-part probe, not a one-part one

The existing probe jobs assert that a non-caller cannot reach a
restricted pod. That is not sufficient for this slice: these six apps
have no in-cluster caller, so 'cannot reach bento-pdf' is equally true
under enforcement, under a missing pod label, and when bento-pdf is
simply down.

The added negative control (an unfenced pod IS still reachable) and
liveness check (bento-pdf answers through traefik) eliminate the two
false-pass paths, so only real enforcement gets a green.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Turn it on and deploy

**Files:**
- Modify: `ansible/roles/k8s/netpol-baseline/defaults/main.yml`

- [ ] **Step 1: Confirm the six apps are healthy before changing anything**

Run: `kubectl -n homelab get pods -l 'app in (bento-pdf,littlelink,speedtest,healthchecks,ical-proxy,code-server)'`
Expected: six pods, all `1/1 Running`. If one is already unhealthy, fix or exclude it first — otherwise the probe's liveness assertion will fail and be misread as a policy fault.

- [ ] **Step 2: Deploy the six app roles FIRST, while the policy is still allow-all**

The label added in Task 2 lives in six pod templates, and `--tags netpol-baseline` does **not**
deploy them — that tag covers only the baseline role. Deploying the flip without this step leaves
zero pods carrying the label, so the policy selects nothing and the probe's assertion 4 fails.

Deploying the label first is also the safer order in its own right: `netpol_baseline_enforced` is
still `false`, so the live policy renders `ingress: [{}]` (allow-all) and the label is inert. Three
of the six (`speedtest`, `healthchecks`, `code-server`) use `strategy: Recreate` on RWO Longhorn
PVCs and will briefly restart — doing that under allow-all separates "the pods restarted" from
"the fencing broke something".

Run: `./scripts/deploy.sh --tags "bento-pdf,littlelink,speedtest,healthchecks,ical-proxy,code-server"`
Expected: six roles deploy, all pods return to `1/1 Running`.

- [ ] **Step 3: Verify exactly six pods carry the label, before anything is enforced**

Run: `kubectl -n homelab get pods -l netpol-baseline=enforced --no-headers | wc -l`
Expected: `6`. If this is `0`, the label did not reach the pod template — stop and fix that before
flipping the flag, because the flip would otherwise produce a policy that fences nothing while
reading as enforced.

- [ ] **Step 4: Flip the variable**

In `ansible/roles/k8s/netpol-baseline/defaults/main.yml`:

```yaml
netpol_baseline_enforced: true
```

- [ ] **Step 5: Dry run**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "netpol-baseline" --check`
Expected: no errors. `--check` runs unlocked.

- [ ] **Step 6: Deploy the baseline**

Run: `./scripts/deploy.sh --tags "netpol-baseline"`
Expected: the probe Job completes and the debug task prints all four lines —
`control ok`, `negative control ok`, `liveness ok`, `isolated: littlelink:3000 unreachable except through traefik`.

Exit **75** means the git-tree lock was busy and **nothing deployed** — retry, do not treat it as a failure.

- [ ] **Step 7: Verify the live policy matches intent**

Run: `kubectl -n homelab get networkpolicy baseline-ingress -o jsonpath='{.spec}'`
Expected: `podSelector` is `{"matchLabels":{"netpol-baseline":"enforced"}}`, `policyTypes` is `["Ingress"]`, and the `from` list has exactly four peers — traefik, the AND'd observability/prometheus pair, and the two `/32` ipBlocks.

- [ ] **Step 8: Re-verify exactly six pods are selected, now that enforcement is on**

Run: `kubectl -n homelab get pods -l netpol-baseline=enforced --no-headers | wc -l`
Expected: `6` — the same count as step 3. A drop here means a pod restarted without its label,
which would silently un-fence that workload.

- [ ] **Step 9: Confirm the six apps still serve through Traefik**

`domain` is a SOPS secret, so it is not available as a shell variable — read the host from the
live IngressRoute rather than hardcoding it:

Run: `kubectl -n homelab get ingressroute littlelink -o jsonpath='{.spec.routes[0].match}{"\n"}'`

Then check all six services are still green end-to-end:

Run: `uv run python scripts/probe.py targets`
Expected: no new DOWN targets among the six.

Note that a `302` to Authelia does **not** prove the backend was reached — the redirect fires in
the middleware, before Traefik proxies. The probe Job's liveness assertion (step 3 of the Job) is
what actually proves Traefik reaches the pod.

- [ ] **Step 10: Commit**

```bash
git add ansible/roles/k8s/netpol-baseline/defaults/main.yml
git commit -m "Enforce the ingress baseline for the six leaf apps

Flipping this one variable is the whole activation, and setting it back
to false is the whole rollback -- deleting the role would not be, since
kubectl apply does not prune and the live policy would keep enforcing
with nothing in the repo describing it.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Answer the spec's two open items

Slice 2 cannot be planned until these are answered. Both are observations against the now-live policy.

**Files:**
- Modify: `docs/networkpolicy-default-deny.md` (record the answers)

- [ ] **Step 1: Determine whether `registry`'s `10.42.1.0/32` is load-bearing**

Run: `kubectl -n homelab get pod -l app=registry -o jsonpath='{.items[0].spec.nodeName}{"\n"}'`

Registry serves containerd over a hostPort on the node it runs on. If it runs on `daniel-box`, only `10.42.0.1/32` can be doing work and `10.42.1.0/32` (which matches neither node's `cni0`, both being `.1`) is vestigial. Record the finding; do **not** edit registry's policy in this slice — that is slice 4.

- [ ] **Step 2: Observe the source IP for pod → MetalLB VIP traffic**

Run: `kubectl -n homelab logs -l app=mosquitto --tail=50 | grep -i "new connection"`
Expected: connection log lines showing the source address zigbee2mqtt arrives from. Compare against `kubectl -n homelab get pod -l app=zigbee2mqtt -o jsonpath='{.items[0].status.podIP}'`.

If they match, ETP=Local preserves the pod IP and a `podSelector` will work in slice 4. If they differ, the VIP SNATs and slice 4 needs an `ipBlock`.

- [ ] **Step 3: Record both answers in the spec**

Replace the "Open items to settle during slice 1" section of `docs/networkpolicy-default-deny.md` with the observed answers and the commands that produced them.

- [ ] **Step 4: Commit**

```bash
git add docs/networkpolicy-default-deny.md
git commit -m "Answer the two open items slice 1 existed to settle

Records what was observed rather than what was assumed, because slice
4's mosquitto rule turns on whether ETP=Local preserves the pod source
IP, and that is not derivable from the manifests.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Amend the spec for the opt-in-label refinement

The spec as approved describes `podSelector: {}` and "one policy per namespace". Planning surfaced that this fences all 60 homelab pods the moment it lands, contradicting slice 1's near-zero blast radius. The design is now a selector that **migrates** from a label to `{}`.

**Files:**
- Modify: `docs/networkpolicy-default-deny.md`

- [ ] **Step 1: Rewrite the "One policy per namespace, not two" section**

Replace the `podSelector: {}` snippet with the `netpol_baseline_scope` form from Task 1 Step 2, and add this paragraph beneath it:

```markdown
### The selector migrates, it does not start wide

`podSelector: {}` fences every pod in the namespace the instant it is applied, which would make
slice 1 a whole-namespace change wearing a six-app label. The selector therefore starts as an
opt-in label (`netpol-baseline: enforced`), each slice labels its own workloads, and a final
slice switches `netpol_baseline_scope` to `namespace`.

That last switch is **slice 5, with its own PR**, and it is the risky one: at that moment every
pod nobody remembered to label gets fenced at once, including anything added between now and
then. It is gated on a check that enumerates pods in the namespace lacking the label and fails
if the list is non-empty, which turns a silent catch-all into an explicit reconciliation.
```

- [ ] **Step 2: Add slice 5 to the slice-plan table**

| # | Scope | Why here |
|---|---|---|
| **5** | Switch `netpol_baseline_scope` to `namespace` | Makes a workload fenced-by-default instead of opt-in. Gated on zero unlabelled pods |

- [ ] **Step 3: Commit**

```bash
git add docs/networkpolicy-default-deny.md
git commit -m "Amend the spec: the baseline selector migrates, not starts wide

Planning slice 1 surfaced that podSelector: {} fences all 60 homelab
pods on apply, which would have made 'slice 1 is six leaf apps' false in
practice. An opt-in label defers that to an explicit slice 5 gated on
there being no unlabelled pods left.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Done when

- `kubectl -n homelab get pods -l netpol-baseline=enforced` returns exactly 6 pods.
- The probe Job prints all four assertions green on a deploy.
- All six apps still serve through Traefik.
- `netpol_baseline_enforced: false` + redeploy demonstrably restores open access (worth doing once, to prove the rollback lever before slices 2-4 rely on it).
- The spec's open items are replaced by observed answers.
