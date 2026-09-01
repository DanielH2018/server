# Zero-Downtime Deploys — Plan 2: Pi-hole redundancy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** LAN DNS survives a Pi-hole deploy. Today a Pi-hole rollout takes every LAN client's DNS down for the length of the gap, because one Deployment backs the only DNS VIP.

**Architecture:** A second Pi-hole Deployment with its own PVC, carrying the same `app: pihole` pod label so the existing Services select both pods, pinned to the same node as the first. The two are restarted in sequence rather than together, so one always serves. No new VIP, no MetalLB change, and no router change.

**Tech Stack:** k3s, Ansible, MetalLB, Jinja2-templated manifests, pytest.

**Spec:** `docs/archive/zero-downtime/design.md` (slice 2)

## The design changed after reading the cluster — read this before the tasks

The spec proposed "a second instance with its own PVC **and its own VIP**", with clients handed both addresses by router DHCP. **That design is wrong for this cluster**, and the tasks below do something simpler instead.

`ansible/roles/setup/k3s/templates/metallb-pool.yaml.j2` announces every VIP from `daniel-box` **only**, and says so in capitals:

> PERMANENT (operator decision 2026-08-14 …): with ETP Local load-bearing for client-IP preservation and two asymmetric nodes, this pin and the VIP-backed workloads' daniel-box nodeSelectors are one unit — never move either alone.

A second Pi-hole on `daniel-server` with its own VIP would need a second `L2Advertisement` pinned to `daniel-server`, reopening the exact failure that decision closed — when `daniel-server`'s speaker won an L2 election, workloads there lost the cluster entirely.

**So both instances go on `daniel-box`, behind the existing VIP.** Both Services already select on `app: pihole` (`service.yaml.j2:11` and `:33`), and the Deployment's pod template already carries that label, so a second Deployment with the same label is picked up with no Service change at all. `externalTrafficPolicy: Local` stays correct because both pods are on the announcing node.

What this buys and what it does not:

- **Buys:** DNS survives a deploy, which is the stated goal and the thing that happens routinely.
- **Does not buy:** node-level redundancy. If `daniel-box` dies, DNS dies — but so do Traefik, MQTT, WireGuard and every other VIP-backed service, all pinned to the same node by the same decision. DNS-specific node redundancy is not meaningfully achievable without revisiting that pin, which is out of scope here.
- **Removes an open question:** spec open question 4 (client distribution across two VIPs, DHCP vs router) disappears. Clients keep one DNS address.

Two preconditions verified before writing this:

- **Pi-hole here is DNS-only, not a DHCP server** (`deployment.yaml.j2:65-66`: "this copy is DNS-only … :67 not exposed"). Two DHCP servers on one LAN would be a serious fault; two DNS resolvers are not.
- **Both containers have a `readinessProbe`**, so a restarting instance drops out of the Service's endpoints instead of blackholing queries.

## Global Constraints

- Ansible is the only write path to the cluster. Plain `kubectl` authenticates as `system:serviceaccount:kube-system:homelab-readonly` (`get list watch` only); `sudo` is denied.
- Manifest templates under `roles/k8s/<role>/templates/` must parse as YAML after rendering — `scripts/validate/validate_k8s_manifests.py` renders every `*.j2` there and asserts it. Multi-document templates are fine; the validator uses `safe_load_all`.
- Tests live in a directory already in `pyproject.toml` `testpaths`; `ansible/tests` is one.
- `pytest` runs with `-n auto`. Tests must be filesystem-read-only and order-independent.
- **A Deployment that is not `manifests_rollout` or in `manifests_extra_rollouts` is never waited on.** This bit slice 1: the rollout completes unwatched and a failure reports green.
- The MetalLB pool is `10.0.0.241-250`; `.241` jellyfin, `.242` mosquitto, `.243` pihole, `.244` wg-easy, `.245` terraria, `.246` valheim are taken. Do not add a VIP in this plan — but if a later change needs one, `.247`–`.250` are free.
- Commits are signed. Never `--no-verify` or `--no-gpg-sign`.
- Run `uv run ansible-playbook` / `uv run pytest`, never the bare binaries.

---

### Task 1: Second Pi-hole instance

**Files:**
- Modify: `ansible/roles/k8s/pihole/defaults/main.yml`
- Modify: `ansible/roles/k8s/pihole/templates/deployment.yaml.j2`
- Modify: `ansible/roles/k8s/pihole/templates/pvc.yaml.j2`
- Modify: `ansible/roles/k8s/pihole/tasks/main.yml`

**Interfaces:**
- Produces: a second Deployment named `pihole-2` and a PVC named by `pihole_k8s_claim_2`, both consumed by Tasks 2–4.
- The pod label `app: pihole` on both instances is load-bearing: it is what makes the existing Services select both. Do not change it, and do not add a distinguishing label the Services select on.

- [ ] **Step 1: Add the second instance's settings**

In `ansible/roles/k8s/pihole/defaults/main.yml`, beside `pihole_k8s_claim`:

```yaml
# Second instance, for deploy-time DNS continuity. Its own claim because /etc/pihole is RWO
# and single-writer; the contents are fully derivable (see the note on pihole_k8s_claim), so
# two independent copies reconciled from the same declaration stay equivalent without sharing
# a volume.
pihole_k8s_claim_2: pihole-etc-2
```

- [ ] **Step 2: Render both PVCs**

`pvc.yaml.j2` currently renders one claim. Make it render both by appending a second document. Keep the existing document byte-identical and add:

```yaml
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ pihole_k8s_claim_2 }}
  namespace: {{ k8s_namespace }}
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: {{ pihole_k8s_storage_class }}
  resources:
    requests:
      storage: {{ pihole_k8s_size }}
```

- [ ] **Step 3: Render both Deployments**

`deployment.yaml.j2` renders one Deployment. Wrap its body in a loop over the two instances rather than copying it — a copied 100-line Deployment will drift.

At the top of the file, immediately after the leading `---`, add:

```jinja
{% for inst in [{'name': 'pihole', 'claim': pihole_k8s_claim},
                {'name': 'pihole-2', 'claim': pihole_k8s_claim_2}] %}
```

and at the very end of the file:

```jinja
{% if not loop.last %}---{% endif %}
{% endfor %}
```

Then inside the body make exactly three substitutions, and no others:

- `metadata.name: pihole` → `metadata.name: {{ inst.name }}`
- the `claimName:` referencing `pihole_k8s_claim` → `claimName: {{ inst.claim }}`
- add, next to the existing `nodeSelector`, a comment recording why both are on one node:

```yaml
      # BOTH instances pin here, deliberately. Every VIP is announced from daniel-box only
      # (setup/k3s metallb-pool.yaml.j2, marked PERMANENT), and externalTrafficPolicy: Local
      # means a pod on the other node would receive nothing. Redundancy here is against a
      # rollout, not against node loss.
```

**Leave `spec.template.metadata.labels.app: pihole` unchanged on both.** That label is what makes the existing Services front both instances. Leave `selector.matchLabels` as it is too — each Deployment selects its own pods by that label plus the ReplicaSet's generated pod-template-hash, so two Deployments sharing an `app` label do not fight over each other's pods.

- [ ] **Step 4: Verify both render**

Run: `uv run python scripts/validate/validate_k8s_manifests.py`
Expected: exit 0.

Run: `uv run python -c "import sys; sys.path.insert(0,'ansible/tests'); sys.path.insert(0,'ansible/filter_plugins'); sys.path.insert(0,'scripts'); from _k8s_render import rendered_docs; print(sorted(d['metadata']['name'] for r,t,d in rendered_docs() if r=='pihole' and d['kind']=='Deployment'))"`
Expected: `['pihole', 'pihole-2']`

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/k8s/pihole
git commit -m "Add a second Pi-hole instance behind the existing DNS VIP

One Deployment backs the only LAN DNS VIP, so every Pi-hole deploy takes DNS
down for the length of its Recreate gap. A second instance with its own PVC,
carrying the same app: pihole label the Services already select on, means one
always serves.

Both pin to daniel-box rather than splitting across nodes: every VIP is
announced from daniel-box only, by a decision the MetalLB template marks
PERMANENT, and externalTrafficPolicy: Local would give a pod on the other node
no traffic at all. This is redundancy against a rollout, not against node loss."
```

---

### Task 2: Restart the two instances in sequence, never together

Two instances only help if they do not go down together. The shared `k8s/manifests` role fires `rollout restart` on its rollout targets back to back and defers all waiting to the end-of-batch drain, so left alone it would restart both Pi-holes within a second of each other and reproduce the outage this plan exists to remove.

**Files:**
- Modify: `ansible/roles/k8s/pihole/tasks/main.yml`

**Interfaces:**
- Consumes: the `pihole` and `pihole-2` Deployments from Task 1.
- Produces: an ordered restart that Task 4 asserts on.

- [ ] **Step 1: Suppress the shared role's automatic restart**

In `ansible/roles/k8s/pihole/tasks/main.yml`, in the `include_role` vars for `k8s/manifests`, add:

```yaml
    # Restarting is done explicitly below, in sequence. The shared role fires its restarts
    # back to back and defers every wait to the end-of-batch drain, which would take both
    # Pi-holes down at once — exactly the outage the second instance exists to prevent.
    manifests_rollout: ''
```

Do **not** add `manifests_extra_rollouts` here; both restarts are handled by the next step.

- [ ] **Step 2: Add the ordered restart**

Immediately after the `include_role` for `k8s/manifests`, add:

```yaml
- name: Roll the Pi-hole instances one at a time
  tags: [deploy]
  # Sequential and each waited on: `rollout status` blocks until the instance is Ready again,
  # so the second instance is only taken down once the first is serving. A parallel restart
  # would leave the Service with no ready endpoint and drop every LAN query in the window.
  ansible.builtin.command:
    cmd: >-
      k3s kubectl -n {{ k8s_namespace }} rollout restart deploy/{{ item }}
  loop:
    - pihole
    - pihole-2
  become: true
  changed_when: true
  notify: []

- name: Wait for each Pi-hole instance to come back before touching the next
  tags: [deploy]
  ansible.builtin.command:
    cmd: >-
      k3s kubectl -n {{ k8s_namespace }} rollout status deploy/{{ item }} --timeout=180s
  loop:
    - pihole
    - pihole-2
  become: true
  changed_when: false
```

**This is wrong as written and Step 3 fixes it** — both restarts fire before either wait. It is split this way so the fix is a deliberate act rather than something to skim past.

- [ ] **Step 3: Interleave the restart and the wait**

Replace both tasks from Step 2 with a single included file so each instance is restarted *and waited on* before the next is touched.

Create `ansible/roles/k8s/pihole/tasks/roll_one.yml`:

```yaml
---
# One instance: restart, then block until it is serving again. Included per instance so the
# next Pi-hole is never taken down while this one is still coming back.
- name: Restart Pi-hole instance {{ pihole_instance }}
  tags: [deploy]
  ansible.builtin.command:
    cmd: "k3s kubectl -n {{ k8s_namespace }} rollout restart deploy/{{ pihole_instance }}"
  become: true
  changed_when: true

- name: Wait for Pi-hole instance {{ pihole_instance }} to be serving again
  tags: [deploy]
  ansible.builtin.command:
    cmd: >-
      k3s kubectl -n {{ k8s_namespace }} rollout status deploy/{{ pihole_instance }}
      --timeout=180s
  become: true
  changed_when: false
```

And in `tasks/main.yml`, replace the two Step-2 tasks with:

```yaml
- name: Roll the Pi-hole instances one at a time
  tags: [deploy]
  ansible.builtin.include_tasks: roll_one.yml
  loop:
    - pihole
    - pihole-2
  loop_control:
    loop_var: pihole_instance
```

- [ ] **Step 4: Lint**

Run: `uv run ansible-lint ansible/roles/k8s/pihole/`
Expected: no errors. If it flags `no-changed-when` on the restart task, the `changed_when: true` above already answers it; do not silence anything else.

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/k8s/pihole
git commit -m "Roll the two Pi-hole instances in sequence, not together

The shared manifests role fires its restarts back to back and defers every
wait to the end-of-batch drain. Applied to two Pi-holes that takes both down
within a second of each other, which is precisely the outage the second
instance was added to prevent — the redundancy would have been decorative.

Each instance is now restarted and waited on before the next is touched."
```

---

### Task 3: Reconcile blocklists into both instances

`tasks/main.yml` reconciles declared adlists and regex denies into gravity.db with `kubectl exec deploy/pihole`. That targets one instance, so the second would drift: same queries answered with different blocking.

**Files:**
- Modify: `ansible/roles/k8s/pihole/tasks/main.yml`

**Interfaces:**
- Consumes: both Deployments from Task 1.

- [ ] **Step 1: Loop the reconcile over both instances**

The existing task runs `k3s kubectl … exec -i deploy/pihole -c pihole -- pihole-FTL sqlite3 …`, registers `pihole_k8s_gravity_sql`, and is `changed_when` its output reports changes. Convert it to a loop over `pihole` and `pihole-2`, replacing the hardcoded `deploy/pihole` with `deploy/{{ item }}` and adding `loop:` with both names.

Because the task now registers a *loop* result, its `changed_when` expression no longer sees `.stdout_lines` directly. Change the register to evaluate per iteration — the existing expression applies unchanged to each `item` within the loop, since Ansible evaluates `changed_when` per iteration against that iteration's result.

- [ ] **Step 2: Loop the gravity rebuild over both instances**

The `Rebuild gravity after a blocklist change` task runs `pihole -g` on `deploy/pihole` when the reconcile changed something. Convert it to the same loop.

Its `when: pihole_k8s_gravity_sql is changed` now refers to a loop result. For a registered loop, `is changed` is true when *any* iteration changed — which is the correct behaviour here: if either instance's declaration changed, rebuild both so they stay equivalent.

- [ ] **Step 3: Verify the rendered commands name both instances**

Run: `grep -n "deploy/{{ item }}\|loop:" ansible/roles/k8s/pihole/tasks/main.yml`
Expected: the reconcile and rebuild tasks both loop over `pihole` and `pihole-2`.

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/k8s/pihole
git commit -m "Reconcile blocklists into both Pi-hole instances

The reconcile and gravity rebuild both targeted deploy/pihole by name, so the
second instance would answer the same query differently — the failure would
show up as intermittent ad-blocking depending on which pod a client landed on,
which is far harder to diagnose than an outage."
```

---

### Task 4: Guard the properties that make this work

Three things silently undo this: dropping to one instance, letting them restart together, and splitting them across nodes. Each fails green.

**Files:**
- Create: `ansible/tests/test_pihole_redundancy.py`

- [ ] **Step 1: Write the failing test**

Create `ansible/tests/test_pihole_redundancy.py`:

```python
"""LAN DNS survives a Pi-hole deploy only while three properties hold.

Each of them fails green — the deploy succeeds and DNS goes down anyway:

  * one instance instead of two: the Service has a single backend and its Recreate gap is
    a LAN-wide DNS outage, which is the state this replaced;
  * both restarted at once: the shared manifests role fires restarts back to back and defers
    waiting to the end-of-batch drain, so a reintroduced `manifests_rollout` would take both
    down within a second of each other and the redundancy would be decorative;
  * split across nodes: every VIP is announced from daniel-box only (marked PERMANENT in
    setup/k3s metallb-pool.yaml.j2), and with externalTrafficPolicy: Local a pod on the other
    node receives nothing, so half the capacity would silently serve no traffic.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from _k8s_render import rendered_docs

_REPO = Path(__file__).resolve().parents[2]
_TASKS = _REPO / "ansible/roles/k8s/pihole/tasks/main.yml"

INSTANCES = {"pihole", "pihole-2"}


def _pihole_deployments() -> dict[str, dict]:
    return {
        doc["metadata"]["name"]: doc
        for role, _tpl, doc in rendered_docs()
        if role == "pihole" and doc.get("kind") == "Deployment"
    }


def test_two_instances_are_rendered():
    assert set(_pihole_deployments()) == INSTANCES


def test_both_instances_are_selected_by_the_dns_service():
    """The Services front both pods only because both carry `app: pihole`."""
    selectors = [
        doc["spec"]["selector"]
        for role, _tpl, doc in rendered_docs()
        if role == "pihole" and doc.get("kind") == "Service" and doc["spec"].get("selector")
    ]
    assert selectors, "no selecting Service found for pihole"
    for name, dep in _pihole_deployments().items():
        labels = dep["spec"]["template"]["metadata"]["labels"]
        for sel in selectors:
            assert all(labels.get(k) == v for k, v in sel.items()), (
                f"{name} is not selected by a pihole Service — it would take no DNS traffic"
            )


def test_instances_do_not_share_a_volume():
    claims = []
    for dep in _pihole_deployments().values():
        for vol in dep["spec"]["template"]["spec"].get("volumes", []):
            if "persistentVolumeClaim" in vol:
                claims.append(vol["persistentVolumeClaim"]["claimName"])
    assert len(claims) == len(set(claims)), (
        f"both instances mount the same RWO claim {claims} — the second pod cannot start"
    )


def test_both_instances_pin_to_the_announcing_node():
    nodes = {
        name: dep["spec"]["template"]["spec"].get("nodeSelector", {}).get(
            "kubernetes.io/hostname"
        )
        for name, dep in _pihole_deployments().items()
    }
    assert set(nodes.values()) == {"daniel-box"}, (
        f"every VIP is announced from daniel-box only and the Service is "
        f"externalTrafficPolicy: Local, so a pod elsewhere receives nothing. Got {nodes}"
    )


def _tasks() -> list[dict]:
    return yaml.safe_load(_TASKS.read_text())


def test_the_shared_role_does_not_restart_pihole():
    """`manifests_rollout: ''` is what stops both instances restarting together."""
    for task in _tasks():
        if task.get("ansible.builtin.include_role", {}).get("name") == "k8s/manifests":
            vars_ = task.get("vars", {})
            assert vars_.get("manifests_rollout") == "", (
                "pihole must set manifests_rollout: '' — otherwise the shared role restarts "
                "both instances back to back and defers waiting to the end-of-batch drain"
            )
            assert not vars_.get("manifests_extra_rollouts"), (
                "extra rollouts restart in a batch too; use the sequenced roll_one.yml instead"
            )
            return
    raise AssertionError("pihole no longer includes k8s/manifests")


def test_the_rollout_is_sequenced_per_instance():
    included = [
        task
        for task in _tasks()
        if str(task.get("ansible.builtin.include_tasks", "")).endswith("roll_one.yml")
    ]
    assert included, "no per-instance roll_one.yml include — restarts are not sequenced"
    looped = included[0].get("loop")
    assert set(looped) == INSTANCES, f"roll_one.yml must cover both instances, got {looped}"
```

- [ ] **Step 2: Run it**

Run: `uv run pytest ansible/tests/test_pihole_redundancy.py -v -n0`
Expected: PASS, 6 tests.

- [ ] **Step 3: Prove each guard catches its regression**

For each of the three, make the change, confirm the named test fails, then restore with `git checkout <file>` and confirm it passes again. **Restore from git after each one, and re-run the whole file before moving to the next** — a leftover mutation silently invalidates the checks that follow.

1. Change one instance's `nodeSelector` to `daniel-server` → `test_both_instances_pin_to_the_announcing_node` fails.
2. Remove `manifests_rollout: ''` → `test_the_shared_role_does_not_restart_pihole` fails.
3. Point both Deployments at `pihole_k8s_claim` → `test_instances_do_not_share_a_volume` fails.

- [ ] **Step 4: Commit**

```bash
git add ansible/tests/test_pihole_redundancy.py
git commit -m "Guard the three properties Pi-hole redundancy depends on

Dropping to one instance, restarting both at once, and splitting them across
nodes all fail green: the deploy succeeds and LAN DNS goes down anyway. The
node pin in particular is not obvious from the pihole role alone — it follows
from an L2Advertisement in a different role that announces every VIP from
daniel-box only."
```

---

### Task 5: Deploy and measure

**Files:**
- Modify: `docs/archive/zero-downtime/design.md` (Measured results)

- [ ] **Step 1: Dry run first**

Run: `uv run ansible-playbook ansible/deploy.yml --tags pihole --check`
Expected: completes. `--check` runs unlocked. Read the diff for anything touching the *existing* instance's volume — this plan must not disturb `pihole-etc`.

- [ ] **Step 2: Start the measurement**

DNS is reachable from the host, so the endpoints workaround slice 1 needed is unnecessary here — measure real queries:

```bash
uv run python scripts/dev/measure_rollout_gap.py --dns homepage.local.<domain> --server 10.0.0.243 --seconds 400 --interval 0.25
```

Resolve `<domain>` from `ansible/inventory/group_vars/all.yml` first. Also start an endpoints poller in a second terminal, so a gap can be attributed to the Service losing all backends versus a query failing for another reason:

```bash
uv run python scripts/dev/measure_rollout_gap.py --endpoints pihole --seconds 400 --interval 0.25
```

- [ ] **Step 3: Deploy**

Run: `./scripts/deploy.sh --tags pihole`

Exit 75 means the git-tree lock stayed busy and nothing deployed — wait and re-run. Confirm afterwards that a rollout actually happened, or the measurement is meaningless: `kubectl -n homelab get pods -l app=pihole` should show two pods with fresh ages and two distinct ReplicaSet hashes.

- [ ] **Step 4: Record the result**

Both probes should report zero failures. Add a row to the *Measured results* table in `docs/archive/zero-downtime/design.md`:

```markdown
| 2 | pihole | YYYY-MM-DD | N | 0 | 0.00s | DNS queries + ready endpoints |
```

If the DNS probe shows failures while the endpoints probe shows a backend throughout, the instances are restarting together — check `roll_one.yml` is actually being included per instance rather than the two restarts firing before the two waits.

Record what was measured, not what was hoped. A failure here is a finding, not a setback.

```bash
git add docs/archive/zero-downtime/design.md
git commit -m "Record the measured Pi-hole redundancy result"
```

---

## What this plan does not cover

- **Template changes still restart both instances at once.** The sequenced restart in
  `roll_one.yml` only gates the `rollout restart` calls — it cannot gate Kubernetes' own
  rollout controller. Both `pihole` and `pihole-2` Deployments render into a single
  `deployment.yaml`, applied in one `kubectl apply -f <dir>/`. A change to
  `deployment.yaml.j2` itself (a Renovate image bump, a resource limit tweak) changes both
  pod templates in that single apply, and the Deployment controller Recreate-cycles both
  instances simultaneously — before `roll_one.yml` runs at all. This plan's redundancy
  covers the common case (ConfigMap/Secret-only changes: blocklist edits, secret rotation,
  which don't touch `deployment.yaml.j2`) but not a template change. The real fix is
  splitting the two Deployments into separately-applied manifest files, so each instance's
  template can change and roll independently; that is future work, not part of this plan.
- **Node-level DNS redundancy.** Out of scope; it requires revisiting the daniel-box announcement pin, which is marked PERMANENT and protects against a failure that has already happened twice.
- **The 14 workloads with no `readinessProbe`** — the other remaining scope item, and independent of this. Its own plan.
- **The Python 3.14 migration**, which is another session's work. Do not touch `ansible/tests/test_host_scripts_py312.py` here; that session deletes it.
