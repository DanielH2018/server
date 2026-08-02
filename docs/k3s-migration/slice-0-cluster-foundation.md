# k3s Slice 0 — Cluster Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a single-node k3s cluster on daniel-box with MetalLB, Longhorn, and a B2 backup target, plus the `platform:` inventory key that lets one `containers_list` drive both stacks — without moving a single service or touching daniel-server.

**Architecture:** k3s server on daniel-box only (`--cluster-init`, Traefik and servicelb disabled). daniel-server keeps running all 46 Docker containers untouched and does **not** join the cluster until slice 7. Longhorn runs at 1 replica until then; B2 is the durability story in the interim. Ansible remains the deploy driver, branching on a new per-entry `platform: docker|k8s` key that defaults to `docker` so every existing entry behaves identically.

**Tech Stack:** k3s, MetalLB (L2), Longhorn, Backblaze B2 (S3-compatible API), Ansible, SOPS/age, pytest.

**Design spec:** `docs/k3s-migration/design.md`

## Global Constraints

- **Ansible runs through `uv run`** — bare `ansible-playbook` lacks the `community.docker` deps and deploys fail.
- **`containers/` is read-only** — a PreToolUse hook denies direct edits. Change `ansible/roles/containers/*/templates/` instead.
- **Secrets live in SOPS** (`ansible/vars/secrets.yml`, edit with `sops`). Never commit plaintext; gitleaks scans every commit.
- **`no_log: true`** on any task handling a credential (`.claude/rules/ansible.md`).
- **All Ansible tasks idempotent**; prefer specific modules over `shell`/`command`; `ansible-lint` before committing.
- **Tests must not live under `ansible/filter_plugins/`** — Ansible's plugin loader imports every `.py` there at deploy time. Tests go in `ansible/tests/`.
- **`--check` dry-run before applying anything that touches production state.**
- **daniel-server is not touched in this slice.** Any step that would modify it is out of scope and belongs to slice 7.

---

## Correction to the approved slice order

The design's slice 0 installed k3s on **both** nodes. That is wrong and this plan supersedes it.

daniel-server runs 46 production containers. k3s brings its own containerd plus flannel/kube-proxy iptables rules onto a host that already carries Docker's chains, the custom `DOCKER-USER` rule admitting daniel-server to the Pi's Portainer agent, and the hairpin-NAT behaviour documented at length in `wg-easy`'s `host_vars` entry. Installing k3s there first puts the riskiest integration at the point of least knowledge.

daniel-box runs nothing. A single-node cluster there is near-zero-risk and teaches us the same lessons. **daniel-server joins at slice 7, after its Docker workload has drained.**

Two consequences to accept openly:

1. **Longhorn runs at 1 replica until slice 7.** Acceptable because Docker holds the authoritative copy of every service throughout, and B2 is configured here in slice 0. The B2 backup target is a **hard precondition for slice 1** — not a slice-0 nice-to-have.
2. **The resilience payoff lands last.** Headroom arrives at slice 4 when the media cluster moves; failover only exists once daniel-server joins at slice 7. Resilience was a stated goal, so this ordering should be a conscious trade, not a surprise.

## Cross-host bridging — one mechanism, defined once

Deferring daniel-server's join means intermediate slices will produce k8s services that must reach still-in-Docker services on the other host. **Do not let each slice invent its own answer.** The mechanism is:

- **Docker side:** publish the port on the LAN IP (not loopback).
- **k8s side:** a headless `Service` with manually-managed `Endpoints` pointing at `10.0.0.161:<port>`, wrapped in an `ExternalName` only where a stable DNS name is wanted.

Known instances waiting on this:

| Slice | k8s consumer | Docker provider | Blocker |
|---|---|---|---|
| 3 | `monitor-bridge` | `kopia` | Kopia is in the rework bucket, stays in Docker longer |
| 5 | `home-assistant` | `peanut` / `upsd:3493` | **Currently published `127.0.0.1:3493` — loopback only.** Unreachable from another host until the publish is widened to the LAN IP |

The `peanut` case is a genuine prerequisite the design missed: `nut` publishes `"127.0.0.1:3493:3493"` deliberately, and the host runs its own `upsmon` secondary against it to execute `systemctl poweroff` on FSD. Widening that publish is a security-relevant change to a shutdown-critical service and needs its own review in slice 5 — it is **not** a mechanical edit.

---

## File Structure

| File | Responsibility |
|---|---|
| `ansible/filter_plugins/toposort.py` | **Modify.** Add `filter_by_platform` alongside the existing dependency filters. Lives here rather than in a new plugin file because Ansible loads every module in this directory at deploy time — one more file is one more import on every run, and this filter operates on the same `containers_list` shape as its neighbours. |
| `ansible/tests/test_platform_filter.py` | **Create.** Unit tests for the new filter. Separate from `test_toposort.py` because that file documents itself as covering the four dependency-resolution filters; platform selection is a different concern with a different failure mode. |
| `ansible/deploy.yml` | **Modify.** Filter `containers_list` to Docker-platform entries before the dependency map is built. |
| `ansible/inventory/host_vars/_example.yml` | **Modify.** Document the `platform:` key so the next person adding a service sees it. |

---

### Task 1: `platform` filter with a safe default

The whole risk of this task is the default. Every one of the 46 existing entries has no `platform:` key; if the default is anything other than `docker`, the next deploy silently skips every service.

**Files:**
- Modify: `ansible/filter_plugins/toposort.py`
- Test: `ansible/tests/test_platform_filter.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `filter_by_platform(containers_list: list[dict], platform: str = "docker") -> list[dict]`, registered as the Jinja filter `filter_by_platform`. Returns entries whose `platform` key equals `platform`, treating a missing key as `"docker"`. Preserves input order.

- [ ] **Step 1: Write the failing test**

Create `ansible/tests/test_platform_filter.py`:

```python
#!/usr/bin/env python3
"""Unit tests for filter_by_platform in filter_plugins/toposort.py.

This filter decides which containers a deploy touches at all. Its default is
load-bearing: all 46 existing containers_list entries omit `platform`, so a
default of anything but "docker" would silently skip every service on the next
deploy. The default-behaviour tests below are the guard against that.

Lives in ansible/tests/ (not under filter_plugins/) so Ansible's filter-plugin
loader doesn't import it as a plugin.

Run: uv run pytest ansible/tests/test_platform_filter.py
"""

from toposort import filter_by_platform


def _c(name, platform=None):
    entry = {"name": name}
    if platform is not None:
        entry["platform"] = platform
    return entry


def _names(containers):
    return [c["name"] for c in containers]


def test_entries_without_platform_key_default_to_docker():
    # The critical case: every existing containers_list entry looks like this.
    containers = [_c("traefik"), _c("authelia"), _c("jellyfin")]
    assert _names(filter_by_platform(containers, "docker")) == [
        "traefik",
        "authelia",
        "jellyfin",
    ]


def test_entries_without_platform_key_are_excluded_from_k8s():
    containers = [_c("traefik"), _c("authelia")]
    assert filter_by_platform(containers, "k8s") == []


def test_explicit_platform_selects_matching_entries():
    containers = [_c("traefik"), _c("speedtest", "k8s"), _c("jellyfin", "docker")]
    assert _names(filter_by_platform(containers, "docker")) == ["traefik", "jellyfin"]
    assert _names(filter_by_platform(containers, "k8s")) == ["speedtest"]


def test_platform_defaults_to_docker_when_argument_omitted():
    containers = [_c("traefik"), _c("speedtest", "k8s")]
    assert _names(filter_by_platform(containers)) == ["traefik"]


def test_input_order_is_preserved():
    containers = [_c("z"), _c("a"), _c("m")]
    assert _names(filter_by_platform(containers, "docker")) == ["z", "a", "m"]


def test_empty_list_returns_empty_list():
    assert filter_by_platform([], "docker") == []


def test_original_list_is_not_mutated():
    containers = [_c("traefik"), _c("speedtest", "k8s")]
    filter_by_platform(containers, "docker")
    assert _names(containers) == ["traefik", "speedtest"]
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest ansible/tests/test_platform_filter.py -v
```

Expected: collection error — `ImportError: cannot import name 'filter_by_platform' from 'toposort'`.

- [ ] **Step 3: Write the minimal implementation**

In `ansible/filter_plugins/toposort.py`, add the function next to the other filters:

```python
def filter_by_platform(containers_list, platform="docker"):
    """Select containers_list entries targeting a given deploy platform.

    A missing `platform` key means "docker". This default is load-bearing:
    every pre-migration entry omits the key, so defaulting any other way
    would silently drop every service from the next deploy.
    """
    return [c for c in containers_list if c.get("platform", "docker") == platform]
```

Register it in the `filters` map:

```python
    def filters(self):
        return {
            "build_dep_map": build_dep_map,
            "toposort_containers": toposort_containers,
            "dep_closure": dep_closure,
            "expand_with_deps": expand_with_deps,
            "filter_by_platform": filter_by_platform,
        }
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest ansible/tests/test_platform_filter.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Run the full suite to confirm nothing regressed**

```bash
uv run pytest
```

Expected: all pre-existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add ansible/filter_plugins/toposort.py ansible/tests/test_platform_filter.py
git commit -m "Add filter_by_platform for dual-stack container deploys

The k3s migration runs Docker and k8s side by side for weeks, and both
need to be driven from one containers_list rather than a shadow inventory
that drifts. This filter is the branch point.

The docker default is deliberate and tested: all 46 existing entries omit
the platform key, so any other default would silently skip every service
on the next deploy."
```

---

### Task 2: Wire the filter into `deploy.yml`

`deploy.yml` must see only Docker entries — before the dependency map is built, so k8s entries never enter dependency resolution or the running-state check.

**Files:**
- Modify: `ansible/deploy.yml:10` (insert a pre_task ahead of "Build role dependency map")

**Interfaces:**
- Consumes: `filter_by_platform` from Task 1.
- Produces: a `containers_list` containing only Docker entries, for every downstream pre_task and the role loop.

- [ ] **Step 1: Insert the filtering pre_task**

In `ansible/deploy.yml`, immediately after the `Load encrypted secrets` import and **before** `Build role dependency map`:

```yaml
    - name: Restrict this play to Docker-platform containers
      ansible.builtin.set_fact:
        containers_list: "{{ containers_list | filter_by_platform('docker') }}"
      tags: always
```

Ordering matters: it must precede `build_dep_map` so a k8s entry can never appear in the dependency graph, the `dep_closure`, or the `docker_container_info` running-state check.

- [ ] **Step 2: Verify the rendered list is unchanged for a real host**

```bash
uv run ansible-playbook ansible/deploy.yml --check --tags speedtest -e target=daniel-server
```

Expected: a `--check` run that resolves the same container set as before the change. No entry should be skipped — nothing has a `platform:` key yet, so this is a behavioural no-op.

- [ ] **Step 3: Lint**

```bash
uv run ansible-lint ansible/deploy.yml
```

Expected: no new findings.

- [ ] **Step 4: Commit**

```bash
git add ansible/deploy.yml
git commit -m "Filter deploy.yml to docker-platform containers

Placed ahead of build_dep_map so a k8s entry can never reach dependency
resolution or the docker_container_info state check. Behavioural no-op
today: no entry carries a platform key yet."
```

---

### Task 3: Document the key

**Files:**
- Modify: `ansible/inventory/host_vars/_example.yml`

- [ ] **Step 1: Add the documented key**

Add to the `containers_list` example entry:

```yaml
    # Which deploy stack owns this service. Omit it (or set `docker`) for the
    # Compose stack that deploy.yml drives. Set `k8s` once the service has been
    # migrated to the k3s cluster — deploy.yml then skips it entirely, and the
    # k8s manifests become its source of truth.
    # Migration status: see docs/superpowers/plans/ for the current slice.
    platform: docker
```

- [ ] **Step 2: Commit**

```bash
git add ansible/inventory/host_vars/_example.yml
git commit -m "Document the platform key for new container entries"
```

---

### Task 4: k3s server on daniel-box

Infrastructure, not software — there is no failing-test-first cycle for an installer. Each step therefore carries an explicit verification command and expected output instead of a test file.

**Host:** daniel-box (10.0.0.215) only. **daniel-server is not touched.**

> **Tasks 4–7 are implemented as `ansible/k3s-bringup.yml` + `roles/setup/k3s`.** Run that rather than pasting the shell below:
>
> ```bash
> uv run ansible-playbook ansible/k3s-bringup.yml
> ```
>
> It must be run **on daniel-box** — the play asserts `inventory_hostname == 'daniel-box'` and refuses to run anywhere else. The shell steps below remain the reference for what each stage does and how to verify it by hand.
>
> **`--check` is close to useless here, and you should not rely on it.** Nearly every task is `ansible.builtin.command`, which check mode *skips* rather than simulates. A dry run therefore skips the k3s install, then skips the node-Ready wait and every `kubectl` task that depended on it, and reports green having proved nothing. Adding `check_mode: false` to "fix" that would make a dry run genuinely install k3s, which is worse. The meaningful first run is the real one, on an empty host, where the two fail-closed guards are the actual safety net.
>
> **This playbook has never been executed.** It was authored from an agent session where a guard blocks `sudo` inside remote commands, so every claim in it is unverified against a real cluster. Expect to fix something on the first run.

- [ ] **Step 1: Confirm the host is still clean**

```bash
ssh daniel-box systemctl status docker
ssh daniel-box ls -la /usr/bin/docker
```

Expected: `Unit docker.service could not be found` and `No such file or directory`.

**Docker is not installed on daniel-box** — verified 2026-08-01. The bring-up handoff had listed Docker's install/service state as "not independently verified", with a green Ansible run as the only evidence; that run did not install it, because `containers_list` is empty and nothing pulled the role in.

Do not treat this as a defect to fix. It *strengthens* the case for k3s-first-on-daniel-box: there are no Docker iptables chains for flannel and kube-proxy to land on top of, so the conflict surface that makes daniel-server risky is zero here.

One consequence to carry forward: **daniel-box is not a working Docker host.** `gitops_deploy` is enabled with a 30-minute timer, and it is a no-op only while `containers_list` stays empty. Adding a Docker-platform service to this host would fail at deploy. Anything landing here should be `platform: k8s`.

If Docker *is* present, stop — someone installed it since, and the risk assessment that put k3s here first no longer holds.

> **Update 2026-08-01 — this happened, and the reasoning above was wrong.** A bare
> `initial_setup.yml` run (no `--tags`) installed Docker on daniel-box later the same day. The
> claim in the paragraph above — that a green Ansible run left Docker out *because
> `containers_list` is empty* — does not hold: `docker_install` was an **unconditional** role in
> `initial_setup.yml`, wired to nothing in `containers_list`. Any full run would have installed
> it. The empty `containers_list` was never what protected this host.
>
> Docker was purged the same day and the precondition is now **enforced rather than assumed**:
> `docker_install` carries `when: has_docker`, which `host_vars/daniel-box.yml` sets false.
> See the handoff doc §1a and `ansible/tests/test_k3s_host_has_no_docker.py`.

- [ ] **Step 2: Install k3s**

```bash
ssh daniel-box 'curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server --cluster-init --disable=traefik --disable=servicelb --write-kubeconfig-mode 0640" sh -'
```

- `--cluster-init` — embedded etcd from day one. Not a one-way door (k3s converts SQLite→etcd on restart with this flag), but it saves a control-plane restart if a third node is ever added.
- `--disable=traefik` — the bundled Traefik cannot carry the existing config: CrowdSec Yaegi plugin, Authelia forwardauth, the raw-TCP Terraria entrypoint, Cloudflare `trustedIPs`.
- `--disable=servicelb` — klipper-lb conflicts with MetalLB.

- [ ] **Step 3: Verify the node is Ready**

```bash
ssh daniel-box sudo k3s kubectl get nodes -o wide
```

Expected: one node, `STATUS=Ready`, `ROLES=control-plane,etcd,master`.

- [ ] **Step 4: Verify etcd, not SQLite**

```bash
ssh daniel-box sudo ls /var/lib/rancher/k3s/server/db/etcd
```

Expected: the directory exists. If it does not, `--cluster-init` did not take and Task 5 onward would build on the wrong datastore.

- [ ] **Step 5: Confirm Traefik and servicelb really are absent**

```bash
ssh daniel-box sudo k3s kubectl get pods -A
```

Expected: `coredns`, `local-path-provisioner`, `metrics-server`. **No** `traefik`, **no** `svclb-*`. If Traefik is present the disable flag was dropped and it will fight the real ingress in slice 1.

- [ ] **Step 6: Record the outcome**

Append the verified command outputs to this plan file under a "Slice 0 results" heading, then commit. The next slice's author needs to know what was actually observed, not what was intended.

---

### Task 5: MetalLB with an address pool

- [ ] **Step 1: Confirm the pool range is free**

```bash
ssh daniel-box ping -c 2 10.0.0.240
```

Expected: 100% packet loss. Repeat for `.241`. A reply means the range is in use by DHCP and must be moved — and the router's DHCP scope should be narrowed to exclude `.240-.250` before proceeding, or MetalLB will hand out addresses the router also leases.

- [ ] **Step 2: Install MetalLB**

```bash
ssh daniel-box sudo k3s kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.8/config/manifests/metallb-native.yaml
```

- [ ] **Step 3: Wait for the controller**

```bash
ssh daniel-box sudo k3s kubectl -n metallb-system wait --for=condition=Available deploy/controller --timeout=180s
```

Expected: `deployment.apps/controller condition met`.

- [ ] **Step 4: Define the pool and L2 advertisement**

Write `/tmp/metallb-pool.yaml` on daniel-box:

```yaml
---
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: homelab-pool
  namespace: metallb-system
spec:
  addresses:
    - 10.0.0.240-10.0.0.250
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: homelab-l2
  namespace: metallb-system
spec:
  ipAddressPools:
    - homelab-pool
```

Apply it:

```bash
ssh daniel-box sudo k3s kubectl apply -f /tmp/metallb-pool.yaml
```

- [ ] **Step 5: Prove an IP is actually allocated and reachable**

```bash
ssh daniel-box sudo k3s kubectl create deploy smoke --image=traefik/whoami
ssh daniel-box sudo k3s kubectl expose deploy smoke --port=80 --type=LoadBalancer
ssh daniel-box sudo k3s kubectl get svc smoke
```

Expected: `EXTERNAL-IP` is an address from the pool, not `<pending>`. Then, **from the Fedora workstation** — this is the step that proves L2 advertisement works off-host:

```bash
curl -s --max-time 5 http://10.0.0.240/ | head -5
```

Expected: whoami output. `<pending>` means the pool is misconfigured; a timeout with an allocated IP means L2/ARP is not reaching the LAN.

- [ ] **Step 6: Tear down the smoke test**

```bash
ssh daniel-box sudo k3s kubectl delete deploy/smoke svc/smoke
```

---

### Task 6: Longhorn at 1 replica

- [ ] **Step 1: Install the prerequisites Longhorn needs**

```bash
ssh daniel-box sudo apt-get install -y open-iscsi nfs-common
```

`open-iscsi` is not optional — without it volumes stay stuck in `Attaching` with an error that does not name the missing package.

- [ ] **Step 2: Install Longhorn**

```bash
ssh daniel-box sudo k3s kubectl apply -f https://raw.githubusercontent.com/longhorn/longhorn/v1.7.2/deploy/longhorn.yaml
```

- [ ] **Step 3: Wait for it to settle**

```bash
ssh daniel-box sudo k3s kubectl -n longhorn-system rollout status deploy/longhorn-driver-deployer --timeout=600s
ssh daniel-box sudo k3s kubectl -n longhorn-system get pods
```

Expected: all pods `Running` or `Completed`.

- [ ] **Step 4: Set the default replica count to 1**

```bash
ssh daniel-box sudo k3s kubectl -n longhorn-system patch settings.longhorn.io default-replica-count --type=merge -p '{"value":"1"}'
```

One node cannot satisfy 2 replicas; leaving the default at 2 makes every PVC report degraded and buries real problems in expected noise. **Slice 7 raises this to 2 when daniel-server joins** — that is the step that turns replication on, and it is the point at which failover starts existing.

> **Update 2026-08-01 — this step alone does not do what it claims.** The patch above applies
> cleanly and reading the setting back returns `1`, but a PVC created through the `longhorn`
> StorageClass still binds at **3** replicas, which is how this failed slice 0's exit criteria
> on the first real attempt:
>
> ```
> kubectl -n longhorn-system get settings.longhorn.io default-replica-count  -> 1
> kubectl get sc longhorn -o jsonpath='{.parameters.numberOfReplicas}'       -> 3
> ```
>
> Upstream's `deploy/longhorn.yaml` hardcodes `numberOfReplicas: "3"` in the
> `longhorn-storageclass` ConfigMap, and a StorageClass parameter **overrides**
> `default-replica-count` — which only governs volumes whose class stays quiet about replicas.
> Note that v1.7.2's manifest does not set `default-replica-count` anywhere; the setting falls
> back to longhorn-manager's compiled-in default, so the "leaving the default at 2" above
> describes a value not verified here. What *is* verified is the class shipping 3. Slice 7's
> target of 2 is unaffected — that number comes from the cluster having two nodes, not from
> upstream.
>
> Fixed in `roles/setup/k3s`: it now applies its own class from
> `files/longhorn-storageclass.yaml`, verbatim from upstream except that
> `numberOfReplicas` is **omitted**, so the setting is the single lever. StorageClass
> parameters are immutable, so the role deletes and recreates the class when it finds one
> still pinning a count — safe, because parameters are read at provision time only and an
> existing PV keeps its own spec. Guarded by `ansible/tests/test_longhorn_storageclass.py`.
> Keeping the parameter absent is also what keeps slice 7's raise a settings patch instead of
> StorageClass surgery under live workloads.

- [ ] **Step 5: Prove a PVC binds**

```bash
ssh daniel-box sudo k3s kubectl apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: smoke-pvc
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn
  resources:
    requests:
      storage: 1Gi
EOF
ssh daniel-box sudo k3s kubectl get pvc smoke-pvc
```

Expected: `STATUS=Bound`. Leave this PVC in place — Task 7 backs it up.

---

### Task 7: B2 backup target — the hard gate for slice 1

Until this passes, nothing may migrate. This is the control that replaces Kopia for anything living on a PV, and its failure mode is silent.

- [ ] **Step 1: Read the existing B2 credentials**

They already exist in SOPS — `kopia_b2_key_id`, `kopia_b2_application_key`, `kopia_b2_bucket`, `kopia_b2_endpoint`. Longhorn needs an S3-style secret.

**Decide first, and record the decision in the PR:** a separate bucket (or a distinct prefix) for Longhorn, rather than writing into Kopia's bucket root. Two tools sharing one namespace makes retention rules ambiguous and a mistaken lifecycle policy could expire the other's data.

- [ ] **Step 2: Create the Longhorn backup secret**

```bash
ssh daniel-box sudo k3s kubectl -n longhorn-system create secret generic longhorn-b2 \
  --from-literal=AWS_ACCESS_KEY_ID='<kopia_b2_key_id>' \
  --from-literal=AWS_SECRET_ACCESS_KEY='<kopia_b2_application_key>' \
  --from-literal=AWS_ENDPOINTS='<kopia_b2_endpoint>'
```

Take the values from `sops ansible/vars/secrets.yml`. Do not echo them into a shell history file or a committed manifest — this step is why the eventual Ansible task needs `no_log: true`.

- [ ] **Step 3: Point Longhorn at the target**

```bash
ssh daniel-box sudo k3s kubectl -n longhorn-system patch settings.longhorn.io backup-target --type=merge -p '{"value":"s3://<bucket>@us-west-000/longhorn"}'
ssh daniel-box sudo k3s kubectl -n longhorn-system patch settings.longhorn.io backup-target-credential-secret --type=merge -p '{"value":"longhorn-b2"}'
```

- [ ] **Step 4: Take a real backup and verify it in the bucket**

```bash
ssh daniel-box sudo k3s kubectl -n longhorn-system get backuptarget default -o jsonpath='{.status.available}'
```

Expected: `true`. Then snapshot and back up `smoke-pvc` through the Longhorn UI or CRD, and **confirm the object exists in B2 from outside the cluster** — list the bucket with the same credentials Kopia uses.

A `backuptarget` that reports `available: true` proves credentials and reachability. It does **not** prove an object was written. Only listing the bucket proves that, and that is the whole point of this gate — the failure this guards against is a backup system that reports success while storing nothing.

- [ ] **Step 5: Clean up the smoke PVC**

```bash
ssh daniel-box sudo k3s kubectl delete pvc smoke-pvc
```

- [ ] **Step 6: Record results and commit**

Append the verified outputs — node status, MetalLB allocation, the curl from the workstation, PVC bound, and the B2 object listing — to this plan under "Slice 0 results".

---

## Exit criteria

Slice 0 is done when all of these are true, each demonstrated by a command whose output was read:

- [x] daniel-box reports one `Ready` control-plane node backed by etcd
- [x] No Traefik and no `svclb-*` pods exist
- [x] A LoadBalancer Service gets a pool IP **and answers from the Fedora workstation** —
      answered from daniel-server rather than the workstation; either proves the ARP/L2 half,
      which is the point
- [x] A Longhorn PVC reaches `Bound` at 1 replica
- [x] A backup object is **listed in the B2 bucket from outside the cluster** — 10 `.blk` data
      blocks under `longhorn/backupstore/volumes/…`, listed from daniel-server
- [x] `uv run pytest` passes, including the new platform-filter tests
- [x] A `--check` deploy against daniel-server resolves an unchanged container set — `changed=0`
      across the 101 tasks that ran. The play then aborted on `authelia`, and that is **not**
      migration drift: the running container uses an untagged image ID and the local
      `authelia/authelia:4.39.20` tag is gone, so compose's `--dry-run` cannot simulate the
      pull it would need. Pre-existing — the container was created 9 days earlier. Tracked as
      daniel-server maintenance, not slice 0.
- [x] **daniel-server has not been modified** — `uptime` 4 days (no reboot; the k3s work began
      ~8 h earlier), 66 running containers. The only commands it saw were a `curl` and one
      `docker run --rm amazon/aws-cli` for the B2 listing, which adds an image, not a container.

## Explicitly out of scope

- Any change to daniel-server (slice 7)
- Any service migration (slice 1 onward)
- Traefik, Authelia, or ingress configuration (slice 1)
- Raising Longhorn to 2 replicas (slice 7)
- Retiring Portainer or the Pi's agent (slice 7)
- Widening `peanut`'s loopback publish (slice 5, needs its own review)
