# k3s Slice 1 — Ingress, SSO, and the First Leaf Service

Stands up **Traefik + Authelia in k3s on a new MetalLB VIP**, with **bento-pdf** behind a test
hostname, and proves the whole chain: routing → TLS → SSO → monitor → backup.

Prerequisite: [slice 0](slice-0-cluster-foundation.md), complete. Cluster is single-node on
daniel-box (`10.0.0.215`), Longhorn at 1 replica with a B2 backup target under the `longhorn/`
prefix, MetalLB pool `10.0.0.240-250`.

Design context: [`design.md`](design.md) §7 (ingress VIP, secrets, coexistence), §8 (slice table
and the Authelia-storage hazard).

---

## What this slice proves, and what it deliberately does not

**Proves:** that a request can enter the cluster on a VIP, terminate TLS with a real
Let's Encrypt certificate, be gated by an Authelia forward-auth Middleware, reach a pod, be
monitored, and have its state land in B2.

**Does not prove, on purpose:**

- **Public reachability.** The router forward stays on `10.0.0.161`. No public DNS record points
  at the new VIP. The k8s stack is reachable **only from the LAN**, via Pi-hole overrides. This is
  what makes it safe to run without CrowdSec.
- **Two-factor.** See *Decision 4*. Slice 1 exercises the `one_factor` LAN path end to end.
- **That the Docker copies can be turned off.** They stay running. That is slice 2.

---

## Global constraints

Same as slice 0, plus two new ones.

1. **daniel-box only.** Nothing in this slice touches daniel-server's Docker stack — with one
   exception, called out explicitly: the Pi-hole DNS override (Task 5) is an Ansible change
   deployed to daniel-server. It adds two `address=` lines and changes nothing else.
2. **`--check` proves nothing here.** Every k8s task is `ansible.builtin.command`/`kubernetes.core`,
   which check mode skips rather than simulates. This bit twice in slice 0. Exit criteria below are
   real `curl`/`kubectl` invocations, never a dry run.
3. **No secret values in manifests committed to git.** Rendered manifests land on the node under
   `/etc/rancher/k3s/` (root-owned) with `mode: '0600'` and `no_log: true` on the task, matching how
   `authelia/configuration.yml` is already rendered in plaintext on daniel-server today.
4. **The k8s stack is not in the blast radius of the Docker stack.** Separate VIP, separate
   Authelia storage, separate ACME account, separate session cookie. If slice 1 breaks, nothing
   currently serving traffic notices.

---

## Decisions settled here

These are the parts `design.md` left open. Each is load-bearing for a later slice.

### 1. Ingress VIP — `10.0.0.240`, reserved structurally

The ingress address must not be claimable by an ordinary `LoadBalancer` Service. Rather than
writing "don't use .240" in a comment, split the MetalLB pool:

- `ingress-pool` — `10.0.0.240/32`, **`autoAssign: false`**. Only claimable by explicit
  `metallb.io/loadBalancerIPs` annotation.
- `homelab-pool` — `10.0.0.241-10.0.0.250`, auto-assigning, for everything else.

`autoAssign: false` is the enforcement: MetalLB will never hand `.240` to a Service that did not
ask for it by name. Use the **`metallb.io/loadBalancerIPs` annotation**, not `spec.loadBalancerIP` —
that field is deprecated upstream and MetalLB reads the annotation.

> The slice-0 smoke Service currently holds `.240`. Delete it first
> (`k3s kubectl delete deploy/smoke svc/smoke pvc/smoke-pvc`) or the pool split won't apply cleanly.

### 2. Certificates — Traefik's own ACME resolver, its own account, staging first

Keep Traefik's built-in `certificatesResolvers.cloudflare` (DNS-01 via `cloudflare_dns_token`)
rather than introducing cert-manager. Reasons: it is the config that already works, DNS-01 needs no
inbound reachability — which is precisely why a LAN-only VIP can still get a real certificate — and
adding cert-manager during a migration that already has enough moving parts is exactly the kind of
substitution `design.md` §7 avoided for secrets.

**Both Traefiks request the same SAN set only if the IngressRoute says so.** In the Docker stack the
wildcard comes from the `labels()` macro's `tls.domains[0].main` / `sans` lines. IngressRoute
expresses this as `spec.tls.domains`, and **omitting it makes Traefik request a certificate for the
literal Host rule instead** — a separate cert per hostname, a separate LE issuance, and different
rate-limit arithmetic. Carry the block across explicitly:

```yaml
  tls:
    certResolver: cloudflare
    domains:
      - main: <domain>
        sans:
          - "*.<domain>"
          - "*.local.<domain>"
```

With that in place both instances share one SAN set. Let's Encrypt allows 5 duplicate certificates
per exact SAN set per week; two instances renewing costs 2. That is fine steady-state and *not* fine
while iterating on a broken config.

**So: point the resolver at the LE staging directory until the chain works, then flip to
production.** Make it a variable, not an edit:

```yaml
# k3s_traefik_acme_ca_server: https://acme-staging-v02.api.letsencrypt.org/directory
k3s_traefik_acme_ca_server: ""   # empty => production
```

`acme.json` lives on its own Longhorn PVC, so flipping staging→production means deleting that one
file, not rebuilding the pod.

### 3. Authelia storage — own PVC, **shared encryption key**

The named hazard in `design.md` §8: *"Do not run the k3s Authelia and the Docker Authelia against
the same storage backend."* Concretely, that backend is **SQLite at `/config/db.sqlite3`** — the
file holding TOTP secrets, WebAuthn devices, and regulation records. Two processes writing one
SQLite file corrupts it.

- **Own storage:** a dedicated Longhorn PVC mounted at `/config`. Nothing is shared with
  daniel-server's bind mount.
- **Same `storage.encryption_key`** — reuse the existing `authelia_storage` secret. This is
  deliberate: it makes the slice-6 cutover a **plain file copy** of `db.sqlite3` with no
  re-encryption step. It also means **no new SOPS entry and no `secret_rotation.py sync`** — worth
  stating, because `authelia_storage` is a `pinned` DANGER secret in
  [`docs/secret-rotation.md`](../../secret-rotation.md) and adding a second one would double that
  procedure's surface for no gain.
- **Same `authelia_secret`** (session secret), same reasoning.

### 4. Session cookie — a distinct **name**, or the two portals fight

This is not in `design.md` and it will bite otherwise. Both Authelias serve the cookie domain
`local.<domain>`. The browser holds **one** cookie per (domain, name) pair — so with the current
`name: 'authelia_session_local'` on both, whichever portal logs in last overwrites the other's
cookie. Sessions are held in-memory per instance (no Redis), so the overwritten side then bounces
the user back to login. Symptom: logging into the k8s portal silently signs you out of everything
on daniel-server.

> **Treat this mechanism as a hypothesis until the exit criterion runs.** Authelia selects a cookie
> config by matching the *request's* domain against `cookies[].domain`, not by which portal issued
> the cookie — both instances match `local.<domain>` for a `*-k8s` host. They never cross in slice 1
> only because the k8s IngressRoute points exclusively at the k8s Authelia Service. The
> "log in on k8s, confirm the daniel-server session survives" check is what actually settles it.
> **Fallback if it fails:** give the k8s instance its own cookie *domain* (`k8s.local.<domain>`),
> which costs a certificate SAN — decided now so it isn't invented under pressure.

Fix in the k8s Authelia's config:

```yaml
session:
  cookies:
    - domain: 'local.<domain>'
      authelia_url: 'https://auth-k8s.local.<domain>'
      name: 'authelia_session_k8s'     # NOT authelia_session_local
```

**TOTP consequence.** A fresh DB means no enrolments, so the public `two_factor` path cannot be
exercised in slice 1 without re-enrolling on a database that gets thrown away at cutover. Don't.
The test hostnames are `*.local.<domain>`, which `access_control` already scores `one_factor` from
RFC1918 — the full redirect → login → session → forward-auth chain runs without TOTP. Storage is
still proven: Task 6 enrols one throwaway TOTP device purely to confirm the PVC and encryption key
work, then discards it. The real user database is copied at slice 6.

### 5. CrowdSec — out of scope

`design.md` schedules the CrowdSec LAPI at **slice 6**. Its acquisition also has to be rebuilt
(Docker socket → file-based over `/var/log/pods`), which is a slice of its own. So the k3s Traefik
ships **without** the bouncer plugin and without `crowdsec@file` on its entrypoints.

This is only acceptable because of the LAN-only constraint. **Adding a public DNS record for a
`*-k8s` hostname before slice 6 would expose an unprotected ingress.** Don't.

### 6. Uptime-Kuma monitor — added by hand, once

AutoKuma reads Docker labels off the socket; there are no Docker labels in k8s, and the AutoKuma
rework is **slice 3**. So slice 1 adds **one HTTP monitor by hand** in Uptime-Kuma against the new
hostname. Note it as a known-temporary mechanism rather than building a bridge that slice 3
replaces.

Related, not slice-1 work: `scripts/diagnostics/probe.py health <svc>` shells to `docker inspect` and will not
work for k8s services. It needs a k8s branch eventually — file it, don't fix it here.

### 7. Manifest rendering — follow the slice-0 convention

Slice 0 renders to `/etc/rancher/k3s/*.yaml` and runs an explicit `k3s kubectl apply -f`. Keep that.

Specifically **do not** use k3s's auto-deploy directory (`/var/lib/rancher/k3s/server/manifests/`):
it re-applies on every k3s restart, which fights Ansible for ownership, and it makes "what is
deployed" depend on file-tree state the playbook doesn't fully control.

Pinned upstream manifests (Traefik CRDs + RBAC) are applied by URL, exactly as slice 0 does for
Longhorn and MetalLB. Only the Deployment/Service/ConfigMap are hand-authored — the CRD set is
large, versioned, and not ours to maintain.

---

## File structure

```
ansible/
  deploy.yml                                         # + second play, platform: k8s
  inventory/host_vars/daniel-box.yml                 # containers_list gains 3 entries
  roles/setup/k3s/
    defaults/main.yml                                # pool split, traefik/authelia versions, ACME CA
    templates/metallb-pool.yaml.j2                   # two pools (Task 1)
  templates/ingressroute.yml.j2                      # the labels() replacement (below)
  roles/k8s/
    manifests/tasks/main.yml                         # render → apply → roll → wait
    traefik/
      files/rbac.yaml                                # upstream ClusterRole, vendored
      templates/{rbac-binding,acme-pvc,static-config,dynamic,deployment,service,cf-token-secret}.yaml.j2
    authelia/
      templates/{pvc,deployment,service,ingressroute,forwardauth-middleware,config-secret}.yaml.j2
    bento-pdf/
      templates/{deployment,service,ingressroute}.yaml.j2
  roles/containers/pihole/templates/dnsmasq.yml.j2   # DNS overrides — deployed to daniel-server
  tests/test_k8s_manifests.py                        # guards (below)
scripts/validate/validate_k8s_manifests.py                    # render-and-parse hook (below)
docs/k3s-migration/slice-1-ingress-sso-leaf.md       # this file
```

The shared role is `manifests`, not `common` as the Docker side names its equivalent:
ansible-lint derives a role's required variable prefix from its directory, and `common_` is
already `roles/containers/common`'s namespace.

`containers_list` keys carry over unchanged — that is the whole point of the `platform` key:

```yaml
containers_list:
  - name: traefik
    platform: k8s
    port: 8080
  - name: authelia
    platform: k8s
    hostname: auth-k8s
    port: 9091
    use_authelia: false
  - name: bento-pdf
    platform: k8s
    hostname: bento-pdf-k8s
    port: 8080
    use_authelia: true
```

`networks:` is absent and unused — k8s has a flat pod network, so the `proxy`/`apps` split
dissolves. `use_authelia` keeps its meaning: it decides whether the IngressRoute carries the
forward-auth Middleware.

---

## Tasks — sequenced as thin, exercisable slices

Each task ends in something you can run. Do not proceed past a failing step.

### Task 1: Split the MetalLB pool and reserve the VIP

Delete the slice-0 smoke resources first, then rewrite `metallb-pool.yaml.j2` as two
`IPAddressPool`s (per *Decision 1*) with the `L2Advertisement` listing both.

```bash
sudo k3s kubectl delete deploy/smoke svc/smoke pvc/smoke-pvc --ignore-not-found
uv run ansible-playbook ansible/k3s-bringup.yml --tags k3s
sudo k3s kubectl -n metallb-system get ipaddresspool -o wide
```

**Prove it:** `ingress-pool` shows `AUTO ASSIGN: false`.

### Task 2: k8s deploy plumbing

Add a second play to `deploy.yml` filtering `platform: k8s`, and `roles/k8s/common` to hold the
render → apply → `rollout status` cycle every service repeats. Guard the play on the host actually
running k3s so it no-ops on daniel-server.

**Prove it:** `uv run ansible-playbook ansible/deploy.yml` on daniel-box reaches the new play and
reports `ok`; the same command on daniel-server skips it and still shows `changed=0`.

### Task 3: bento-pdf in the cluster — no ingress yet

Deployment + Service only. Carry across the security posture from the compose template, which is
already tight and translates directly: `readOnlyRootFilesystem`, `drop: [ALL]`,
`allowPrivilegeEscalation: false`, and `emptyDir` volumes for `/var/cache/nginx` and
`/etc/nginx/tmp` (the compose `tmpfs` entries).

The `mode=1777` on those compose entries does **not** translate one-to-one: `emptyDir` also mounts
`0755` root-owned, and the image runs as nginx-unprivileged UID 101, so it will `EPERM` exactly as
it would have on a default tmpfs. Set `securityContext.fsGroup: 101` on the pod so the volumes come
up group-writable by the running user.

```bash
sudo k3s kubectl run curl-probe --rm -it --image=curlimages/curl --restart=Never -- \
  -sS -o /dev/null -w '%{http_code}\n' http://bento-pdf.default.svc.cluster.local:8080/
```

**Prove it:** `200`, with no ingress in the path at all.

### Task 4: Traefik on the VIP, plain HTTP

Apply pinned upstream CRDs + RBAC by URL. Hand-author the static config (a ConfigMap, adapted from
`traefik.yml.j2`), Deployment, and a `LoadBalancer` Service annotated onto `10.0.0.240`.

Adapt, don't copy, the static config:

- **Drop** the `docker` provider; add `providers.kubernetesCRD`.
- **Drop** `crowdsec@file` from both entrypoints (*Decision 5*) and the `experimental.plugins`
  block with it.
- **Keep** `forwardedHeaders.trustedIPs` and its reasoning verbatim — the analysis in that comment
  (why not `10.0.0.0/8`, why not the docker ranges) is about X-Forwarded-For trust and is unchanged
  by the platform.
- **Drop** the `terraria` entrypoint; nothing on this VIP serves it.
- **Keep** `metrics.prometheus`, and the `ping` entrypoint — the latter becomes the pod's readiness
  probe, which is cleaner than the Docker healthcheck.

Add the bento-pdf IngressRoute on the `web` entrypoint only, no TLS yet.

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H 'Host: bento-pdf-k8s.local.<domain>' http://10.0.0.240/
```

**Set `externalTrafficPolicy: Local` on the Traefik Service, and treat it as a security control.**
This is the slice-0 `RemoteAddr` trap, and it is worse than a verification nuisance. Under the
`Cluster` default the node masquerades inbound traffic, so Traefik sees every client as cni0's
`10.42.0.1` — which is inside `10.0.0.0/8`, so **Authelia's `access_control` rules would score all
traffic, including public traffic after slice 6, as RFC1918 LAN** and hand it the `one_factor`
policy meant for the local network. `Local` preserves the real client address. Safe here because
Traefik is the only ingress on a single-node cluster; revisit when daniel-server joins at slice 7.

**Then prove it from daniel-server, not daniel-box.** With `Local` the source address is real, so
an on-host curl and a LAN curl are finally distinguishable — but only a curl from another machine
proves L2 advertisement works at all.

### Task 5: DNS overrides, then TLS

Two lines in Pi-hole's `dnsmasq.yml.j2` — dnsmasq prefers the most specific `address=` match, so
these override the existing `address=/local.<domain>/{{ server_ip }}` wildcard for these two names
only:

```
address=/bento-pdf-k8s.local.<domain>/10.0.0.240
address=/auth-k8s.local.<domain>/10.0.0.240
```

Deploy to daniel-server (`--tags pihole`). Then add TLS to the IngressRoute — with the explicit
`spec.tls.domains` block from *Decision 2*, not just `certResolver` — and mount
`cloudflare_dns_token` as a **file-backed Secret** —
`CF_DNS_API_TOKEN_FILE`, not `CF_DNS_API_TOKEN`, keeping the token out of `kubectl describe` for
the same reason the compose template keeps it out of container metadata — and give `acme.json` its
own Longhorn PVC.

**Run against LE staging first.** Once a staging cert appears, delete `acme.json`, clear
`k3s_traefik_acme_ca_server`, and let it re-issue against production.

```bash
dig +short bento-pdf-k8s.local.<domain>                    # expect 10.0.0.240
curl -sS -o /dev/null -w '%{http_code}\n' https://bento-pdf-k8s.local.<domain>/
echo | openssl s_client -connect bento-pdf-k8s.local.<domain>:443 2>/dev/null \
  | openssl x509 -noout -issuer -dates
```

**Prove it:** `200`, issuer is Let's Encrypt **production**, and `curl` does not need `-k`.

### Task 6: Authelia with its own storage

PVC → Secrets (config + users database, both `no_log: true`) → Deployment → Service →
IngressRoute for `auth-k8s.local.<domain>`.

Config is adapted from `configuration.yml.j2` with three changes and nothing else: the cookie block
from *Decision 4*, `authelia_url` pointing at the new portal, and the `identity_providers.oidc`
clients trimmed to nothing — no OIDC client points at this instance yet, and carrying dead client
registrations into a throwaway database is how stale redirect URIs survive a migration.

Keep `access_control` **byte-identical** to daniel-server's. It is the security policy; slice 1 is
not the place to redesign it.

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://auth-k8s.local.<domain>/
sudo k3s kubectl exec deploy/authelia -- ls -l /config/db.sqlite3
```

**Prove it:** portal returns `200` and renders; log in as a real user; enrol one throwaway TOTP
device to confirm the PVC and `storage.encryption_key` work, then delete it.

### Task 7: Gate bento-pdf behind forward-auth

The `authelia@docker` middleware becomes a `Middleware` CRD — this is the substitution that repeats
~33 more times in slice 2, so get the shape right:

```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: authelia
spec:
  forwardAuth:
    address: http://authelia.default.svc.cluster.local:9091/api/authz/forward-auth
    trustForwardHeader: true
    authResponseHeaders:
      - Remote-User
      - Remote-Groups
      - Remote-Name
      - Remote-Email
```

Attach it to bento-pdf's IngressRoute alongside a `rate-limit` Middleware — the docker `labels()`
macro applies `rate-limit@file` to every authed router, and dropping it silently would remove
brute-force protection.

```bash
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' https://bento-pdf-k8s.local.<domain>/
```

**Prove it:** unauthenticated request returns `302` to `auth-k8s.local.<domain>` (**not**
`auth.local.<domain>` — a redirect to the Docker portal means the config still carries
daniel-server's `authelia_url`); after logging in, the browser reaches bento-pdf.

### Task 8: Uptime-Kuma monitor

Add one HTTP monitor by hand against `https://bento-pdf-k8s.local.<domain>/`, expecting `302`
(it is behind forward-auth now, so `200` is the wrong assertion and will flap).

**Prove it:** monitor is green in Uptime-Kuma.

### Task 9: The backup gate

**bento-pdf is stateless** — read-only rootfs, tmpfs only, no volumes. It contributes nothing to
back up. (`speedtest` would have been the same.) The state slice 1 actually creates is
**Authelia's `/config` PVC** and **Traefik's `acme.json` PVC**, so that is where the §6 gate lands.

Add a Longhorn `RecurringJob` (daily, `backup` task) in the **`default` group**.

Two things about the mechanism, both discovered while implementing it. A RecurringJob does not take
a list of PVC names — it selects volumes by group. And **the labels it reads live on Longhorn's own
`Volume` CRs, not on the PVC**: a `recurring-job-group.longhorn.io/...` label on the PVC does not
propagate and is silently inert, which is a backup that never runs while looking configured. The
`default` group sidesteps both: Longhorn applies it to any volume with no job of its own, so a new
PVC is covered without anyone remembering to label it. The cost is that opting *out* becomes the
explicit act — slice 4's media volumes are large and already covered by Kopia on host paths, so they
will need their own group.

Trigger one backup immediately, then verify from **outside the cluster**; asking Longhorn whether
its own backup exists is not independent evidence, which is the whole point of this gate:

```bash
uv run python scripts/diagnostics/probe.py b2-longhorn   # run on daniel-server
```

**Prove it:** `.blk` data blocks exist alongside `backup_*.cfg` for the **Authelia** volume. Metadata
without blocks is the "reports success while storing nothing" failure this gate exists to catch.

Only the Authelia volume is load-bearing. `acme.json` is regenerable — losing it costs one ACME
re-issue, not data — so it is backed up for tidiness, and a failure there is not a slice blocker.

---

## Tests to add (`ansible/tests/k8s/test_k8s_manifests.py`)

Per the repo's escalation ladder — a check a machine enforces beats a paragraph an agent has to
remember:

1. `ingress-pool` is a `/32` with `autoAssign: false`, and `homelab-pool` does **not** contain the
   ingress address. Guards *Decision 1* against a future pool edit.
2. The k8s Authelia's session cookie `name` differs from the Docker one, and its storage path is
   backed by a PVC. Guards *Decisions 3 and 4* — the two silent-corruption cases.
3. Every k8s IngressRoute whose `containers_list` entry has `use_authelia: true` carries both the
   forward-auth and rate-limit Middlewares. This is the check that scales to slice 2's ~33
   hand-authored routes.
4. The k8s Traefik static config contains no `crowdsec` reference while `k3s_crowdsec_enabled` is
   false — so slice 6 turning it on is a deliberate flip, not a silent gap nobody notices.

Plus a `validate-k8s-manifests` prek hook mirroring `validate-compose`: re-render every
`roles/k8s/*/templates/*.yaml.j2` and fail on malformed YAML. Jinja indent bugs in k8s manifests
fail exactly as quietly as they do in compose files, and `ansible-lint` misses both.

It also parses the YAML **embedded in ConfigMap and Secret values**. The manifest wrapping
Traefik's static config and Authelia's `configuration.yml` is valid whatever those block scalars
contain — they are opaque to the outer document — so an outer-only check would miss precisely the
bugs that matter most. It caught one in the `ingressroute()` macro on its first run.

---

## Exit criteria

Slice 1 is done when every one of these has been run and its output read. No `--check` substitutes.

- [x] `k3s kubectl -n metallb-system get ipaddresspool` — `ingress-pool` is `/32`, `autoAssign: false`
      → `ingress-pool  [10.0.0.240/32]  autoAssign false`
- [x] `k3s kubectl get svc traefik` — `EXTERNAL-IP` is `10.0.0.240`
- [x] `curl -H 'Host: bento-pdf-k8s.local.<domain>' http://10.0.0.240/` **from daniel-server** → `200`
      → **`301`**, and that is correct: this criterion was written before Traefik's HTTP→HTTPS
      redirect existed. Re-run over HTTPS from daniel-server: bento-pdf-k8s `302`, auth-k8s `200`.
- [x] `dig +short bento-pdf-k8s.local.<domain>` → `10.0.0.240`
- [x] `openssl s_client` — certificate issued by Let's Encrypt **production**, not staging
      → `issuer=C = US, O = Let's Encrypt, CN = YR2`, valid to 2026-10-31
- [x] `curl https://bento-pdf-k8s.local.<domain>/` unauthenticated → `302` to `auth-k8s.local.<domain>`
- [x] Browser: log in at the k8s portal, reach bento-pdf, **and confirm an existing daniel-server
      session is still valid** — the cookie-collision check from *Decision 4*
      → **Passed 2026-08-02.** Both directions exercised: a Docker login does not disturb the k3s
      session and vice versa; `authelia_session_local` and `authelia_session_k8s` coexist on
      `local.<domain>`. Decision 4's hypothesis holds — the distinct cookie *name* is sufficient
      and the fallback cookie *domain* is not needed.
- [x] TOTP enrolment succeeds and survives an Authelia pod restart (proves the PVC, not just the pod)
      → **Passed 2026-08-03.** Normal use will never exercise this: `*.local.<domain>` is
      `one_factor` from RFC1918, so logging in never touches TOTP, and the only `two_factor` rule
      is the public `*.<domain>` one with `k8s_public_route` false. So it was done deliberately —
      enrol a throwaway device from the portal's settings, `kubectl delete pod -l app=authelia`,
      then generate a code on the enrolled device against the new pod. **The code validated.**
      That is the strongest form of this test: not "the row is still in the table" but "a secret
      written by the previous pod was read back and used successfully by a fresh one", which
      exercises the Longhorn PVC and the shared `storage.encryption_key` together. The new pod
      remounted the same `authelia-config` PVC and served the portal at `200`.

      **Trap for whoever repeats this.** The notifier is `filesystem`
      (`/config/notification.txt`), not email. Authelia 4.39 gates adding a 2FA device behind an
      identity-verification code delivered *through the notifier* — so the code is written to a
      file inside the pod and nobody ever receives mail. Read it with
      `sudo k3s kubectl -n homelab exec deploy/authelia -- tail -20 /config/notification.txt`.
- [x] Uptime-Kuma monitor green
- [x] `b2-list-longhorn.sh` shows `.blk` blocks for the Authelia volume
      → **Passed 2026-08-03.** The original script lived only in an agent scratch directory and
      was deleted with it, so this was unverifiable for a while; rebuilt as
      `uv run python scripts/diagnostics/probe.py b2-longhorn` (run on daniel-server), which reports data
      blocks vs metadata per volume and exits non-zero if any volume has metadata but no blocks.
      `pvc-4ee9f2af…` = `authelia-config`: **11 `.blk` blocks**, 2 cfg. `pvc-1f29a849…` =
      `traefik-acme`: 7 blocks. Both volumes' Aug-3 backups are real data, not just metadata.

      **Finding, not a slice-1 blocker — orphaned backup sets accumulate in B2.** That run
      listed **five** volumes while only **two** Longhorn volumes existed at the time. Three are backups of
      PVCs that have since been deleted: two are the previous authelia/traefik PVCs (recreated
      during the from-scratch Authelia verification and the acme.json wipe) and one is slice 0's
      smoke volume. Longhorn's `retain:` is per-volume, so once a volume is gone nothing prunes
      its backups — they stay in B2 indefinitely. Currently trivial (~0.1 MB each), but it is
      unbounded growth with no reaper, on a bucket at 60% of a 10 GB free tier, and slice 2
      recreates a PVC per migrated service. Worth a cleanup path before that.
- [x] `uv run pytest` and `prek run --all-files` both clean → 1339 passed
- [x] daniel-server carries no *unexpected* change: `docker ps -q | wc -l` matches whatever it was at
      slice-1 start, and `uptime` shows no reboot (the count is a snapshot, not an invariant — it
      legitimately moves as services are added)
      → **Passed 2026-08-03, with one half proven and the other half only partly.** No reboot:
      `uptime -s` is `2026-08-02 07:37:10`, and the first slice-1 PR merged at 12:53 UTC that day,
      so the boot predates every slice-1 change and nothing since has restarted the host.
      Containers: **66** running, all healthy.

      The count half is **unverifiable as written** — no baseline was recorded at slice-1 start,
      so "matches whatever it was" has nothing to match against. Stating that rather than
      back-filling a number nobody measured. For slice 2, capture the count *before* touching
      anything; a criterion that depends on a baseline has to record the baseline to mean
      anything.

---

## Explicitly out of scope

| Deferred to | What |
|---|---|
| Slice 2 | The other ~33 leaf services; stopping any Docker copy |
| Slice 3 | AutoKuma rework; `probe.py health` gaining a k8s branch |
| Slice 6 | CrowdSec LAPI + bouncer; router forward → VIP; public DNS; copying the real Authelia DB; the `two_factor` public path |
| Slice 7 | Second Longhorn replica (still 1 until daniel-server joins) |

**Known risk carried forward:** Longhorn and Kopia share the `daniel-server-kopia` bucket,
separated only by the `longhorn/` prefix. Nothing enforces that a future lifecycle rule stays
prefix-scoped. Flagged in slice 0, still open, and slice 1 is the first time real service state
depends on it.
