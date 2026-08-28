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

daniel-server has VT-x and `/dev/kvm`, 25 GB free RAM, 8 cores and 295 GB free disk. It carried
**no hypervisor** when this was written — no qemu, libvirt, multipass or incus — so Phase A
installs one. `roles/setup/hypervisor` owns it, and it is the only host with `has_hypervisor:
true`. The role is reached from `initial_setup.yml` alone, and because `hosts.ini` pins
daniel-server to `ansible_connection=local`, applying it means running Ansible **on that host**
— `-e target=daniel-server` from daniel-box runs the whole play locally and builds a second guest
there.

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

So: staging runs on a **libvirt NAT network**, reachable from daniel-server only. **Every deploy
to `daniel-stage` therefore runs from daniel-server, not only the fence probe below** — the guest
is a genuinely remote host, and Ansible run from daniel-box fails at the connection with
`ssh: connect to host 192.168.140.10 port 22: Connection timed out`. That recap exits 4 for
hosts unreachable, which is a different exit 4 from `deploy.sh`'s stale-tree refusal. Its
Traefik is
reached on the VM's NAT address. MetalLB still runs — the manifests reference it, and removing it
would make staging less like prod in the manifests, which is what the gate reads — but it
announces only inside the VM's own network.

The cost is honest and bounded: staging does not exercise L2 announcement or externalTrafficPolicy
behaviour. Neither is what a manifest-correctness gate is for.

**NAT is not isolation, and treating it as isolation left a hole open from 2026-08-27 until
2026-08-28** — measured the day it opened, and open for a further day because the first fix was
inert. `<forward mode='nat'/>` constrains nothing about the destination, so the
guest reached the whole production LAN — and reached it *masqueraded as daniel-server*, a trusted
node. Probed from inside the guest: the MetalLB VIP answered 301, the k3s API answered 401, and
daniel-pi's wg-easy admin UI answered 200. The SNAT is what makes this worse than ordinary
reachability. Production's source-IP controls see daniel-server, not staging, so authelia's
`policy: bypass` rules scoped to `lan_subnet` admit the guest and wg-easy's unauthenticated admin
UI rests on a LAN-only premise the guest sits inside.

The fence is a **libvirt nwfilter**, `staging-egress-fence`, rendered by
`roles/setup/hypervisor/templates/staging-nwfilter.xml.j2` and referenced from the guest's
`<interface>`. One drop rule per production range: `lan_subnet`, `k3s_pod_cidr` and
`k3s_service_cidr`.

**The LAN rule alone left the cluster network open, and no LAN rule can close it.** That was the
remaining shape of the hole, closed later the same day. kube-proxy's DNAT runs in the `nat`
PREROUTING chain on daniel-server, which sees forwarded packets, so a guest whose default route
crosses that host reaches a production ClusterIP without any of the traffic being addressed to the
LAN. Measured from inside the guest: Longhorn's API answered 200 unauthenticated on its ClusterIP,
carrying a mutating
`diskUpdate` action, and a `longhorn-ui` pod IP answered 200 directly — while the LAN rule
correctly refused the MetalLB VIP. `k3s_pod_cidr` and `k3s_service_cidr` are declared in
`group_vars/all.yml` rather than the k3s role's defaults, because the consumer is the hypervisor
role and a role's defaults are not visible to another role in another playbook.

Those two ranges are also **staging's own**, since both clusters run k3s's defaults. Dropping them
is safe only while staging is a single node: its pod and Service traffic is delivered on the
guest's internal `cni0` and by its own kube-proxy rules, and never crosses the tap device the
filter attaches to. A second staging node would put pod-to-pod traffic on the wire, where the `/16`
drop would break it. `ansible/tests/test_staging_egress_fence.py` ties that assumption to
`k3s_agent_node_ips` so it fails when it stops holding.

**The first attempt was a UFW `route deny`, and it was inert.** It deployed cleanly and
`ufw status` listed it; the probe still reached the VIP, the k3s API and wg-easy from inside the
guest. The proof it could never have worked is one line of `/etc/default/ufw`:
`DEFAULT_FORWARD_POLICY="DROP"` was already set, so had the UFW forward chain governed this traffic
the guest would have been fenced before the rule was written. libvirt's own FORWARD accept is
reached first. nwfilter removes the ordering question instead of arguing it — libvirt attaches the
rules to the guest's tap device and manages their position itself, which is the same path its stock
`no-ip-spoofing` filter uses on a NAT network.

Two mechanical consequences worth knowing. libvirt applies a filter when the **interface is
created**, so adding the `<filterref>` reaches a running guest only after the domain is stopped and
started — `guest.yml` detects a live interface without the fence and does exactly that. Editing a
**rule** inside an already-referenced filter needs no restart; libvirt re-applies it live. Measured
2026-08-28 when the two cluster CIDRs were added: the play reported two changed tasks and no
`virsh destroy`, guest uptime was unbroken, and both cluster targets went from 200 to refused.

Guest-to-daniel-server traffic is unaffected: the guest reaches this host on the staging gateway
address, which is not in `lan_subnet`. Ansible still reaches the guest, and the internet egress
staging legitimately needs is untouched, because the filter drops one destination rather than
naming an allowlist.

The acceptance gate runs **inside the guest**, not on the host — a fence that is present and inert
reads identically to a working one from the host side, which is the whole reason the first attempt
survived a deploy:

```bash
uv run python scripts/diagnostics/staging_egress_probe.py   # on daniel-server
```

The internet control target must stay reachable. A fence that severed all egress would make every
production target fail too, and that would read as a pass.

The two cluster targets carry a **second control leg**, dialled from daniel-server. Their
addresses are allocated rather than pinned — a pod IP is ephemeral and Longhorn's ClusterIP is
whatever the API server handed it — so a target that has simply moved answers nothing from
anywhere, which is indistinguishable from a fence that works. Silent from both sides exits 2, not
0. The two ClusterIPs that *are* pinned in inventory were rejected as targets for the same reason
in reverse: both already answer nothing from the guest, so either would have read as a held fence
from the day it shipped and could never have gone red.

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

**Revised 2026-08-28, before building it: no CA, and no separate hostname space.** Staging drops
the certificate resolver and lets Traefik serve its own default self-signed certificate. Nothing
on staging needs to *trust* a certificate — the gate asks whether manifests render, apply,
schedule, bind volumes, pass probes and complete a rollout, and a probe answers all of that with
`curl -k`. An internal CA buys trust nothing reads, at the cost of a keypair, a Secret, a TLSStore
exception the `ingressroute()` macro deliberately avoids, and a rotation story.

Staging also keeps prod's exact hostnames (`<svc>.local.<domain>`) rather than a `stage.local`
space. Identical names make the gate measure the real manifests instead of a staging variant, and
reachability comes from `curl --resolve <name>:443:<staging VIP>` against staging's own Traefik —
so no DNS record exists anywhere, and reaching staging by accident takes an explicit `--resolve`.

The mechanism is one variable, `k8s_tls_cert_resolver` in `group_vars/all.yml`: `cloudflare` by
default, empty on `daniel-stage`. Every service route flows through the `ingressroute()` macro, so
one variable covers them all; four hand-rolled routes name it directly.

**The `tls:` key is never conditional, and that is the whole trap.** An IngressRoute on the `https`
entrypoint with no `spec.tls` is not a route that loses — it is a NON-TLS router, never a candidate
for an HTTPS request. It applies cleanly, `kubectl get` shows it, Traefik logs nothing, and it cost
most of 2026-08-07. Wrapping the whole block in the conditional is the obvious implementation and
reintroduces that failure on staging only, where nobody is looking. So an empty resolver drops
`certResolver` and `domains` and keeps the key. ENFORCED by
`ansible/tests/test_tls_cert_resolver_optional.py`, which renders both branches — the existing
manifest guard renders prod's variables only, and never reaches the branch where the mistake lives.

`domains:` goes with the resolver rather than with TLS: it is the SAN set ACME is instructed to
request, so with no resolver it instructs nothing.

Staging therefore does not prove certificate issuance works. That is a deliberate trade:
certificate issuance is the part of the ingress chain least likely to break from a manifest edit,
and the most dangerous to rehearse against a shared rate limit. Every staging probe also runs with
`-k`, so a probe cannot assert anything about the certificate itself.

**TLS is not the whole of this decision.** Measured 2026-08-28: `roles/k8s/traefik`'s templates
reference **eight** SOPS keys, and `secrets-staging.yml` carries one (`domain`). Beyond ACME's
`cloudflare_dns_token` and `email`, the role also needs `crowdsec_k8s_agent_password` and
`crowdsec_k8s_bouncer_api_key` for the bouncer on the edge, `homelab_mcp_token` and
`livesync_sync_token` for routers inside the LiveSync gate Secret, and
`monitor_bridge_cloudflare_drift_push_token` for the Cloudflare-IP drift cron. Solving TLS alone
still leaves five undefined variables, so those subsystems get `traefik_k8s_manage_*` flags in the
shape of `k3s_manage_backup_targets` — staging declines work that only makes sense against prod.

### 5. Secrets — a separate SOPS file, no real credentials

Staging gets `ansible/vars/secrets-staging.yml`, encrypted to **daniel-server's age key**, with
generated values throughout.

An earlier draft named the staging VM's own key plus daniel-box's. Both halves were wrong, and
the mechanism is why: `pre_tasks/load_secrets.yml` decrypts with `delegate_to: localhost`, so
the **controller** decrypts and the values reach the target as play variables. The VM's key is
never consulted, and the controller for a staging play is daniel-server — the only host that
can route to the guest (*Decision 2*) — not daniel-box.

Two consequences to carry into the work:

- `.sops.yaml`'s single creation rule already matches `vars/secrets-staging.yml` by path, so a
  naive `sops` create encrypts it to all four prod recipients — the opposite of this decision.
  It needs a second, narrower rule placed **first**.
- Loading `secrets.yml` at all puts every prod credential in the staging play's variable
  *scope*, even when no role templates one onto the guest. "No role happens to reference it" is
  weaker than this decision promises, so the file the preamble loads has to be chosen by host.
- **No `diff=sops` entry in `.gitattributes` for the staging file.** That attribute makes git
  decrypt before diffing, and only `ansible/vars/secrets.yml` carries it. Adding one for
  `secrets-staging.yml` would break `git diff` on daniel-box, which is not a recipient and
  cannot decrypt it — and buys nothing, because the values are generated and fake. The staging
  file diffs as ciphertext on purpose.

Measured on 2026-08-27, and it makes the first staging slice much smaller than expected: **with
`k3s_manage_backup_targets` and `k3s_manage_health_crons` both false, `roles/setup/k3s` consumes
no secrets at all.** Every secret-bearing template in that role belongs to one of those two
gated task files, and the only other consumer is `k3s-bringup.yml`'s agent-join play, which a
single-node cluster never runs. The guest also has passwordless sudo, so `become_password` is not
needed either — the preamble's `when: become_password is defined` skips cleanly without it. So
`secrets-staging.yml` starts with `domain` alone, which is what the 93 service templates need,
and grows when *Decision 6*'s subset lands.

It has grown once, and the shape of that growth is the pattern to repeat. Authelia added six
keys — `authelia_user`, `authelia_password`, `authelia_jwt`, `authelia_secret`,
`authelia_storage` and `email` — and no more, because the three the OIDC provider block would
need are gated out instead (*Decision 6*). **Prefer a per-cluster flag over a generated credential**: a
flag removes the configuration as well as the secret, where a fake value leaves dead config
that can still fail at startup for a reason that is not a bug.

Two mechanics of editing this file, both of which cost a session time to rediscover:

- **Only daniel-server can write it.** It is encrypted to that host's key alone, so `sops` on
  daniel-box fails, reporting that no identity matched any of the recipients. Generate and re-encrypt
  over ssh there, then commit the ciphertext from wherever you are working. Encrypting is not
  the constrained half — any host can encrypt to a public key — but `sops set` and `sops` both
  decrypt first, so both need the private half.
- **`authelia_password_hash` is deliberately absent.** The role reads the argon2 hash back from
  the cluster or mints it in a one-shot pod, then `set_fact`s it, so no cluster stores it. What
  the file holds is the plaintext the hash is minted from, which is read by `tasks/main.yml`
  and by no template — a shape `test_staging_manifests_have_their_variables.py` was blind to
  until it grew a tasks-file scan.
- **`email` is in the file because the guard cannot see that it is missing any other way.** It
  is not a credential and prod keeps it in `secrets.yml`, which is why staging had no source
  for it at all. `BASE_CONTEXT` in `scripts/lib/_render_guard.py` carries a stand-in so the
  structural validator never aborts, and the guard read that stand-in as staging supplying the
  name — so the check passed and the deploy failed one task later with `'email' is undefined`.
  The guard now builds its supplied set from the real sources and never from `BASE_CONTEXT`.

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
| `authelia` | Middleware wiring and the forward-auth chain — `freshrss` carries `use_authelia: true` here for exactly that reason. Its OIDC provider is gated out by `authelia_k8s_manage_oidc: false`: the only relying party is `jellyfin`, which is hardware-blocked from this subset, so the block would be dead configuration demanding three more fake credentials. Forward-auth is unaffected by that flag |
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

**DONE — this decision describes work that has already landed, and the census above is history
rather than a plan.** `k8s_primary_node` is declared in `group_vars/all.yml` and overridden to
`daniel-stage` in that host's vars; `k8s_registry_node`, `docs_k8s_node` and `artifacts_k8s_node`
all derive from it, and `seed_volume_node` is resolved live from the volume's own attachment, so
it was never cluster-specific. Re-censused 2026-08-28: no `kubernetes.io/hostname` pin outside an
excluded role still names a cluster node literally.

Three literals survive and are correct where they are, so don't "finish" them:

- `nut` and `media-volume` pin `daniel-server` and are permanently excluded (*Decision 6*).
- Every `hostvars['daniel-pi']` lookup — `monitor-bridge`, `uptime-kuma`, `artifacts`,
  `pi-peer-backup`, `pihole` — names the Pi rather than a cluster node, and none of those roles
  is in the subset.
- `registry`'s agent-half pull self-test pins `daniel-server`. That one was a real single-node
  blocker, and it was not a pin problem: the Job proves a SECOND node's containerd reaches the
  registry, so on one node it is unschedulable and the role's `kubectl wait` stalls for its full
  180s timeout. It now renders only when `k3s_agent_node_ips` is non-empty, and the wait task
  gates on the same variable. ENFORCED by `ansible/tests/test_registry_selftest_single_node.py`.

**Decision 4 was the remaining gate on the first staging service, and it landed on 2026-08-28.**
It was unimplemented as this spec was first written: `roles/k8s/traefik` templated a
`cloudflare_dns_token` Secret that `secrets-staging.yml` does not carry, and
`dashboard-ingressroute.yaml.j2` named `certResolver: cloudflare` unconditionally, so a staging
deploy either failed on the undefined variable or requested certificates for the **real** domain
against the ACME account and rate limit prod uses.

What closed it is a set of per-cluster switches, each defaulting to prod's behaviour so that a
staging cluster turns pieces **off** in its own `host_vars` rather than prod turning them on:
`k8s_tls_cert_resolver` (#522, empty on staging), `traefik_k8s_manage_acme` and
`traefik_k8s_manage_cloudflare_drift_check` (#523), `traefik_k8s_manage_crowdsec` and
`authelia_k8s_manage_crowdsec` (#525), `traefik_k8s_manage_livesync_gate` (#526), and
`traefik_k8s_watched_namespaces` (#528). Traefik itself reached staging in #527, `freshrss` in
#529 behind `freshrss_k8s_manage_seed`, and #530 turned off the pre-apply Longhorn snapshots.

**Staging serves Traefik's built-in default certificate**, which is also the discriminator for
which cluster answered: `secrets-staging.yml` carries the same `domain` as prod, so staging's
routes match the names DNS points at prod and only the resolver separates them. A probe written
against a hostname silently measures the wrong cluster; `CN = TRAEFIK DEFAULT CERT` is what says
staging answered. Nothing named `stage.local` or a self-signed CA exists in the repo, and none is
needed for this.

`traefik` still has to precede `freshrss`, because its CRDs back the IngressRoute; the subset table
in *Decision 6* is ordered by mechanism coverage, not by deploy order.

### 8. Three hazards that would otherwise reach prod data

Each would fire on a staging deploy if inherited unchanged. The first is closed, the third is
closed, and the second is closed by construction but not yet exercised — `ical-proxy` is still
slice 6.

**`seed_volume_source_host: daniel-server`** — `seed-volume` copies a source directory from that
host into a PVC. A staging deploy inheriting this default reads **prod's bind-mount data** over
ssh. Closed by skipping seeding rather than pointing it elsewhere:
`freshrss_k8s_manage_seed: false` on staging (#529), with the role rendering its own PVC in place
of the one `seed-volume` creates.
Skipping is the right shape because seeding is a one-shot migration mechanism and staging has
nothing to migrate.

**`k8s_registry_node`** — the in-cluster registry is pinned to one node, so staging's 7 built
images have nowhere to push unless that pin follows the cluster. Step 1 made it a variable
(`k8s_registry_node: "{{ k8s_primary_node }}"`), and staging sets `k8s_primary_node: daniel-stage`,
so the pin follows the cluster without a second override. Staging runs its own registry. The
alternative — pushing staging builds to the prod registry — would let a staging build overwrite a
tag prod pulls, which is a staging change causing a prod rollout.

**Pre-apply Longhorn snapshots** — `k8s/manifests` snapshots a role's PVCs before applying, so a
rollback has something to return to. On staging that is wrong twice over: the volumes hold nothing
worth keeping, and the snapshot task resolves its deploy tag with `git rev-parse` under
`chdir: "{{ playbook_dir }}/.."`, which runs **on the target**. daniel-box is `connection=local`
and has its checkout there; `daniel-stage` is genuinely remote and has none, so the task failed
with `Unable to change directory before execution`. Closed by
`k8s_autodeploy_snapshot_pvcs: []` on staging (#530). The underlying `chdir` is still wrong for
any genuinely remote target and has 13 callers; the fix is `delegate_to: localhost`.

---

## Sequencing

Vertical slices; each leaves something exercisable. Status as of 2026-08-28.

1. **Node-pin variables** (*Decision 7*) — DONE. Repo-only, verified against prod.
2. **Hypervisor + VM** (*Decisions 1, 2*) — DONE. `virsh list` shows a running guest; ssh reaches it. The egress fence landed here and was corrected twice; *Decision 2* carries both.
3. **k3s + Longhorn + Traefik on the VM** (*Decision 3*) — DONE. `kubectl get nodes` is Ready; a PVC binds.
4. **Inventory + staging secrets** (*Decision 5*) — DONE. `ansible -m ping` reaches `daniel-stage`; secrets decrypt.
5. **First service deployed** (*Decisions 4, 6, 8*) — DONE. `freshrss` answers 200 through the staging VIP, serving `CN = TRAEFIK DEFAULT CERT`.
6. **The rest of the subset** — `node-exporter` and `authelia` are DONE; `ical-proxy` remains. `traefik` landed in slice 5, which it had to precede.

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
