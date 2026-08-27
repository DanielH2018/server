# Staging Cluster — a second k3s the GitOps pipeline must pass through

Stands up a **single-node k3s VM on daniel-server** and teaches the repo to deploy to it, so a
bad merge fails on staging before prod ever renders it.

This is **Tier 2** of the staging spike. [Tier 1](deploying.md#checking-a-change-without-deploying-it)
landed on 2026-08-16 as `k8s_dry_run` (PR #237): it validates manifests against the live API
server without applying them. Tier 1 catches bad apiVersions, schema drift and CRD ordering. It
cannot catch scheduling, PVC binding, probe or rollout behaviour, because nothing is ever
actually run. Tier 2 is what runs it.

Covers **Phase A** (the cluster exists) and **Phase B** (Ansible can deploy to it). Phase C —
gating the GitOps pipeline on a staging deploy — gets its own spec once staging has run long
enough to show what a realistic health gate looks like.

---

## What this proves, and what it deliberately does not

**Proves:** that a manifest set renders, applies, schedules, binds its volumes, passes its
probes and completes a rollout — on a cluster that is not prod, from the same Ansible source
prod is deployed from.

**Does not prove, on purpose:**

- **Anything about a service outside the staging subset.** A gate over a subset gates only that
  subset. See *Decision 6*; this is the single most important limitation of the whole design and
  the one most likely to be forgotten once the tile is green.
- **MetalLB L2 behaviour.** Staging sits behind NAT and announces nothing on the LAN. See
  *Decision 2*.
- **Real DNS, real certificates, or real credentials.** Staging touches none of them
  (*Decisions 4 and 5*).
- **Hardware-dependent behaviour.** No UPS, no Intel GPU, no physical disks, no LVM media
  volume — six roles are permanently out of scope (*Decision 6*).

---

## Phase A — the cluster exists

### 1. Substrate — a KVM VM under libvirt, not a container

daniel-server has VT-x and `/dev/kvm`, 25 GB free RAM, 8 cores and 295 GB free disk. It has **no
hypervisor installed today** — no qemu, libvirt, multipass or incus — so Phase A installs one.

A VM rather than the lighter container options, because of what else lives on that box:

- **LXD/Incus system container** — k3s in a container shares the **host kernel**: the same
  netfilter tables, the same cgroup tree, the same kernel modules that prod's k3s *agent* uses on
  this host. A staging cluster that can perturb the host's iptables is a staging cluster that can
  take prod down.
- **k3d** — needs Docker, which was deliberately removed from daniel-server on 2026-08-14 and is
  fail-closed guarded in `roles/setup/k3s`. Re-adding it to get staging would undo a migration
  decision.

A VM gets its own kernel and its own network stack. That isolation is the entire reason staging
is safe to break, and it is worth the RAM.

**Sizing:** 8 GB RAM, 4 vCPU, 100 GB disk. Leaves daniel-server ~17 GB, which is above its
current 4 GB working set with room for the k3s agent's own growth.

**Node name:** `daniel-stage`. This name is load-bearing — see *Decision 7*.

### 2. Network — libvirt NAT, and no MetalLB on the prod L2

**Staging must not be able to hurt prod, and sharing the LAN segment is the one design where it
can.** Prod's ingress VIP is `10.0.0.240` with a MetalLB pool of `10.0.0.241-250`; `.251-.254`
are free, so a bridged staging with its own pool is *possible*. It is still the wrong choice.

MetalLB L2 election on this segment has already produced two recorded outages
(`etp-local-vips-blackout-on-node-join`), both of which presented as host probes staying green
while traffic blackholed. A second MetalLB speaker on the same L2, announcing from a VM that
exists to be broken, puts a prod-wide outage downstream of a staging experiment.

So: staging runs on a **libvirt NAT network**, reachable from daniel-server only. Its Traefik is
reached on the VM's NAT address. MetalLB still runs — the manifests reference it, and removing it
would make staging less like prod in the manifests, which is what the gate reads — but it
announces only inside the VM's own network.

The cost is honest and bounded: staging does not exercise L2 announcement or externalTrafficPolicy
behaviour. Neither is what a manifest-correctness gate is for.

### 3. Cluster stack — k3s, Longhorn, Traefik, in that order

Single-node, so Longhorn runs at 1 replica. The existing `roles/setup/k3s` is the source for the
k3s install itself; Phase A's job is to make that role's assumptions parameterisable rather than
to write a second one.

The storage class is `longhorn`, same as prod, so the 3 manifests that name it explicitly and the
20 that rely on the default both resolve the way they do on prod.

---

## Phase B — Ansible can deploy to it

Phase B is the harder half. It is where the repo stops assuming there is exactly one cluster.

### 4. DNS and TLS — staging touches neither Cloudflare nor prod Let's Encrypt

Staging IngressRoutes must not request certificates from the same ACME account as prod, and must
not create or update a Cloudflare record. Two independent hazards:

- Prod uses Traefik's `cloudflare` DNS-01 resolver. Pointed at staging, it would issue against
  the **real** domain and consume the same rate limit.
- `cloudflare-ddns` mutates real DNS unconditionally. It is permanently excluded (*Decision 6*).

Staging serves `*.stage.local.<domain>` from its own Traefik with a **self-signed internal CA**,
generated at bring-up and trusted only inside the VM. No ACME, no rate limit, no external
dependency, and nothing a staging misconfiguration can leak into the real zone.

A self-signed CA means staging does not prove certificate issuance works. That is a deliberate
trade: certificate issuance is the part of the ingress chain least likely to break from a manifest
edit, and the most dangerous to rehearse against a shared rate limit.

### 5. Secrets — a separate SOPS file, no real credentials

Staging gets `ansible/vars/secrets-staging.yml`, encrypted to the **staging VM's own age key**
plus daniel-box's, with generated values throughout.

**Staging never holds a real credential.** A host whose stated purpose is being broken, and which
Claude sessions are expected to experiment on, is the worst possible second copy of every
production secret. Roles whose function depends on a real external credential (B2, Cloudflare,
healthchecks.io) are excluded rather than given a real key.

Consequence to accept up front: a role that fails on staging *because* its credential is fake is
a false failure. The subset in *Decision 6* is chosen partly to avoid that class entirely.

### 6. The staging subset — five services, and the hole this leaves

Staging cannot carry all 51 services in 8 GB, and **a gate over a subset gates only that subset.**
This is stated here, in the spec, because it is the claim most likely to be quietly overread once
the pipeline is green.

The initial subset is chosen for coverage of **mechanisms**, not importance:

| Service | Mechanism it exercises |
|---|---|
| `traefik` | CRD installation and ordering — the failure that breaks every route behind it |
| `authelia` | Middleware wiring and the forward-auth chain |
| `freshrss` | A plain web app: Deployment, Service, IngressRoute, PVC, seeding |
| `ical-proxy` | The `image-builder` path end to end — build, push, pull, deploy (*Decision 8*) |
| `node-exporter` | A DaemonSet: per-node scheduling and hostPath mounts, which no Deployment exercises |

`ical-proxy` is the smallest of the seven built images and needs no external credential; the
others are either hardware-blocked (`nut`), Pi-targeted (`pi-peer-backup`), or heavy enough that
a build failure would dominate the staging window (`n8n` + `n8n-runners`, `code-server`).
`node-exporter` is the only DaemonSet that is neither hardware-blocked (`scrutiny`,
`dri-device-plugin`) nor dependent on a credential or an external sink (`claude-otel`,
`loki-homelab`, `crowdsec`) — and its hostPaths (`/proc`, `/sys`) exist in a VM unchanged.

**Permanently excluded, with the reason:**

- **Hardware-blocked (6):** `scrutiny` (`/dev/nvme0`), `nut` (`/dev/bus/usb`),
  `dri-device-plugin` (Intel GPU), `media-volume` (LVM PV), and `jellyfin` / `pihole` /
  `mosquitto` (claim real LAN IPs).
- **Mutates something real:** `cloudflare-ddns` (real DNS records).
- **Needs a real external credential:** anything reaching B2 or healthchecks.io.

Growing the subset later is a config change, not a redesign — but each addition needs the same
question asked: does this role mutate anything outside the VM?

### 7. Node pinning — the 25 sites that name daniel-box

92 occurrences of `daniel-box` appear across the k8s templates, but most are comments. An
earlier draft of this spec put the load-bearing ones at roughly 17. Measured on 2026-08-27
while executing this step, the real count is **25**:

| Kind | Count |
|---|---|
| `kubernetes.io/hostname` nodeSelector pins, as rendered | 14 |
| affinity list entries | 4 |
| `hostvars[...].server_ip` lookups | 5 |
| `hostvars[...].containers_list` lookups | 2 |

Two things make the count harder to take than it looks, and both were why the earlier draft
was low. **A literal grep finds 9 pins, not 14**, because four roles already read a variable
(`docs_k8s_node`, `artifacts_k8s_node` and their kin) rather than the hostname; and **pihole
renders one template twice** through a Jinja `for` loop, so a rendered census
counts it twice where a source census counts it once. Neither census alone is right — reconcile
the two. The `containers_list` lookups were missed entirely.

Each becomes a variable resolving to the deploying cluster's node. This is mechanical but wide,
and it is the change most likely to break prod while building staging — a mis-edited pin moves a
prod workload to the wrong node. It is therefore done **first**, on its own, verified against prod
with `--dry-run` and a real deploy before staging is ever pointed at.

### 8. Two hazards that would otherwise reach prod data

Both are live today and would fire on the first staging deploy if inherited unchanged.

**`seed_volume_source_host: daniel-server`** — `seed-volume` copies a source directory from that
host into a PVC. A staging deploy inheriting this default reads **prod's bind-mount data** over
ssh. Staging must set this to a staging-local path, or skip seeding entirely. Skipping is
preferred: seeding is a one-shot migration mechanism, and staging has nothing to migrate.

**`k8s_registry_node`** — the in-cluster registry is pinned to one node, so staging's 7 built
images have nowhere to push unless that pin follows the cluster. Step 1 made it a variable
(`k8s_registry_node: "{{ k8s_primary_node }}"`), which resolves to `daniel-box` on prod;
staging must override it rather than inherit it. Staging runs its own registry on `daniel-stage`.
The alternative — pushing staging builds to the prod registry — would let a staging build
overwrite a tag prod pulls, which is a staging change causing a prod rollout.

---

## Sequencing

Vertical slices; each leaves something exercisable.

1. **Node-pin variables** (*Decision 7*) — repo-only, verified against prod. Nothing staging-specific yet.
2. **Hypervisor + VM** (*Decisions 1, 2*) — `virsh list` shows a running guest; ssh reaches it.
3. **k3s + Longhorn + Traefik on the VM** (*Decision 3*) — `kubectl get nodes` is Ready; a PVC binds.
4. **Inventory + staging secrets** (*Decision 5*) — `ansible -m ping` reaches `daniel-stage`; secrets decrypt.
5. **First service deployed** (*Decisions 4, 6, 8*) — `deploy.sh --tags freshrss --target staging` completes and the route answers inside the VM.
6. **The rest of the subset** — `traefik`, `authelia`, `ical-proxy`, `node-exporter`.

Phase C (pipeline gating) starts after 6 has run against real merges for long enough to know its
false-failure rate.

## Open questions for Phase C, recorded now

Not decisions for this spec — recorded so Phase C does not rediscover them.

- What counts as a staging pass. `probe.py health` per service is the obvious gate and now
  covers Deployments and DaemonSets, but the pass criteria for a whole-cluster deploy is a
  different question.
- How does a staging failure alert, and who overrides it when staging is wrong rather than the
  merge? A gate with no override becomes a gate that gets removed.
- How much of the 30-minute GitOps window a staging pass costs. A full prod deploy of all 54
  services measures **20m12s** (re-measured 2026-08-22 after the batching work landed;
  `deploy-time-is-83-percent-waiting`), so a five-service subset is a small fraction of the
  window rather than a doubling of it. An earlier draft of this line read 59 minutes and
  concluded the window "roughly doubles" — that figure predates batching, and the conclusion
  drawn from it was wrong. Measure the staging pass before sizing the window against it.
