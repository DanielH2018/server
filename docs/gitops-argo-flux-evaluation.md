# Argo CD / Flux evaluation for this homelab

> **Status, 2026-08-21.** §8's recommendation is done: PR #303 taught
> `scripts/validate_k8s_manifests.py` to schema-check the docs it already renders, and wired it
> into the prek hook. That was the one *new capability* the Kustomize port would have bought, so
> the port is now purely optional — which is what §8 concluded it should become. Everything else
> here is an evaluation, not a plan of record: no Flux controller is installed and none is
> scheduled.

**Recommendation: augment with Flux, in narrow slices, and keep Ansible.** Move the *apply*
of non-secret k8s manifests to Flux's kustomize-controller; keep Ansible as the *renderer*,
the host-config plane, and the secret plane. Do not replace `gitops_deploy.py` wholesale —
its 784 lines of decision logic encode roughly ten recorded incidents, and most of them have
no controller-native equivalent.

Argo CD is the wrong fit here for two measurable reasons, not for taste: it has no native
SOPS support (upstream documents Sealed Secrets / External Secrets / a vault plugin instead),
and its default footprint is 256Mi request for repo-server plus 1Gi for the application
controller, before Redis. `daniel-box` sits at 65% memory (18.8 GiB of 28) today. Flux's
controllers run ~30 MiB each and it decrypts SOPS/age natively.

---

## 1. The decision is where the render boundary sits, not which controller

Argo-vs-Flux is downstream of one choice. Three options:

| Option | Who renders Jinja | Who applies | Notes |
|---|---|---|---|
| **1. Today** | Ansible on the host | Ansible (`kubectl apply -f dir/`) | Controller is moot |
| **2. Render-then-reconcile** | Ansible, in CI or on-box | Flux, from a rendered-YAML branch | Jinja survives; controller sees plain YAML |
| **3. Native port** | Helm/Kustomize, by the controller | Flux/Argo | 297 templates rewritten |

**Option 3 is out as a big bang, but it slices per role — see §8.** 297 `.j2` templates across
52 services, plus `lookup('file')`/`lookup('template')` calls that inline app config into
ConfigMaps and repo-wide vars from `group_vars/all.yml`. Attempted all at once it is months of
work whose only product is a different templating language. Attempted one role at a time, on
top of option 2, it buys three things Jinja cannot (offline schema validation, Renovate's
built-in kustomize manager, and no renderer for that role) — and the Jinja tail never has to
be finished.

**Option 2 is viable, and one measurement is what makes it viable.** Every Jinja conditional
in `roles/k8s/*/templates/` reads static repo data — `group_vars`, `host_vars`, or
`hostvars['daniel-box'].containers_list`. Across all 297 templates only two `ansible_*` facts
appear (`ansible_user`, `ansible_host`), and the single `lookup('pipe')` is a `base64 -w0` of
a checked-in icon file. Nothing reads live host state. A renderer with the repo and the
inventory can therefore produce byte-identical manifests off-host.

---

## 2. What Ansible keeps, whichever way this goes

The boundary is exactly `roles/k8s/**`. Everything else stays, and this is not a consolation
prize — it's most of the plane:

- k3s install, kubeconfig, the read-only ServiceAccount, health crons, etcd snapshots
- systemd units and timers, netplan, NUT and the UPS shutdown chain
- SOPS host onboarding (`bootstrap.yml`, `sops updatekeys`)
- `daniel-pi`'s Docker services entirely
- `roles/k8s/image-builder` — it applies a build Job, reads registry digests before and
  after, and queues a rollout when a mutable tag's digest moved. A controller reconciling a
  Deployment whose spec did not change has no way to notice a rebuilt image. Roles that build
  rather than pull (n8n, n8n-runners, code-server) stay on the Ansible path.
- `roles/k8s/seed-volume` — it mutates PVC contents, not API objects.

---

## 3. Mechanism-by-mechanism: what Flux takes over, what it can't

Ordered by what you'd actually gain first.

### Wins — Flux does this natively and better

| Today | Under Flux | Why it's better |
|---|---|---|
| `kubectl apply -f dir/` leaves orphaned live objects; `manifest-prune-check.sh` cron flags them by hand | `prune: true` — inventory-based GC | The retirement becomes complete. Removing a manifest deletes the object. Retires the cron and the recorded claude-otel-ingest resurrection class. |
| Stale Secret keys survive an apply; `verify_secret_keys.yml` reconciles them explicitly | Same inventory GC | Retires a task written against a `kubectl apply` limitation that Flux does not share. |
| 59-minute full deploy, 83% of it fixed pauses and serial rollout waits | Independent controllers reconciling in parallel, each `Kustomization` on its own interval | The 1561s of `pause` and the batched rollout queue exist because Ansible is serial. Flux is not. |
| Order enforced by hand-position in `containers_list` (no toposort; traefik CRDs before IngressRoutes, seed-volume before its 25 dependents) | `dependsOn` between Kustomizations | Declared, not positional. A misordered list entry stops being a silent failure mode. |
| Per-role auto-deploy opt-out: `k8s_autodeploy` declarations, a derived denylist, a stale-denylist cross-check, and a disarm path (`filter_plugins/k8s_autodeploy.py`, 170 lines, plus its half of `deploy_logic.py`) | `spec.suspend: true` on that service's Kustomization | The whole "config on the host is behind origin" hazard disappears, because there is no rendered config on the host to be behind. |
| Nothing detects live drift between deploys | Continuous drift detection and correction | New capability. Today a hand-run `kubectl patch` survives until the next deploy of that role. |
| `REQUIRE_CI` gate: an unauthenticated check-runs poll, fail-closed handling, per-SHA alert throttling | CI *is* the renderer — a red run produces no new commit on the deploy branch | The gate becomes structural instead of a poll. This is the cleanest single retirement in the list. |

### Losses — no native equivalent, needs a shim or gets dropped

| Today | Flux reality | Options |
|---|---|---|
| Health gate → `git reset --hard` → redeploy prior version → `hold_sha` marker | **A `Kustomization` has no rollback.** Upstream documents `healthChecks`, `wait`, `retryInterval` and drift correction; there is no revert-to-previous-revision on failed health checks. Rollback in Flux is a HelmRelease feature only. The idiom is "revert the commit." | Accept the idiom (a bad revision keeps retrying while alerting), *or* keep a thin rollback shim that force-pushes the deploy branch back one commit. The shim is small — the git logic is already written. |
| `hold_sha` — never redeploy this SHA | No equivalent. | Falls out with the above. Note the existing doc already says a k8s rollback is local-only and the bad pin stays on master; the real fix was always a revert on the remote. |
| `k8s_stabilise_gate` — post-Available soak on container restart delta, catching a bad *liveness* probe that readiness hides | Flux `healthChecks` waits on the same readiness-derived conditions `kubectl rollout status` uses. Same blind spot. | Keep the check, move it out of the play: a `monitor-bridge` check comparing restart counts against Flux's `lastAppliedRevision` timestamp. This one is worth preserving — it caught kube-state-metrics on 2026-08-07. |
| `manifests_image_changed` — rebuild behind a mutable tag triggers a rollout | No equivalent for Ansible-built images. | Those roles stay on Ansible (see §2). |
| Discord alerting on every deferral class, behind-origin watchdog, divergence watchdog | Flux emits events and Prometheus metrics; alerting is via `notification-controller` Alert/Provider objects. | A real port, not a loss — but it is work, and the watchdogs for "host tree behind origin" become meaningless, which is a win. |

---

## 4. Secrets — the constraint that shapes the phasing

Today: one SOPS file (`ansible/vars/secrets.yml`, ~140 keys) decrypted at render time on the
host, interpolated into 18 `secret.yaml.j2` templates, written 0600 with `no_log`. Rendered
output is plaintext, so it can never reach git.

Flux decrypts SOPS/age natively in kustomize-controller — but only if the *encrypted Secret
manifests* live in git, one per service. That inverts the single-file model and breaks
`scripts/secret_rotation.py`, `ansible/secret_rotation.yml`, and the `sops updatekeys`
onboarding flow. There is a tempting middle path — CI renders the secret templates and
re-encrypts them for the cluster's age key — but it requires GitHub Actions to hold a key that
decrypts every secret you own. Today no secret material leaves the two hosts. Don't trade that
for convenience.

**So: secrets stay on the Ansible path.** Ansible continues to render and apply the 18 Secret
objects; Flux owns the Deployments that mount them. This works because Flux prunes only what is
in its own inventory, and a Secret it never applied is not in it. It is a deliberate phase-1
decision, not an omission — revisit it only if the per-service-encrypted-Secret model ever
becomes worth the rotation-registry rewrite.

---

## 5. Renovate does not change, and Flux image automation is not wanted

Renovate bumps `*_image:` vars in each role's `defaults/main.yml`, and those flow through the
renderer unchanged. Flux's image-reflector / image-automation controllers would write image
tags straight to the deploy branch, bypassing the PR and its CI. That is strictly worse here —
the PR *is* the soak. Leave them uninstalled.

---

## 6. Vertical slices

Each slice is exercisable on its own. The trap that governs every one: **a service moving to
Flux must leave `containers_list` in the same change**, or Ansible and Flux fight over the
same objects and the staged files under `/etc/rancher/k3s/manifests/` trip the prune check.

**Slice 1 — Flux installed, owning nothing.** Bootstrap Flux's controllers via an Ansible
role (`roles/k8s/flux`), pointed at a `deploy` branch that is empty. Verify: controllers ready,
memory measured, `flux check` green, notification-controller posting to the existing Discord
webhook. Nothing else changes; the current pipeline still runs everything.

**Slice 2 — the renderer.** A CI job (or an Ansible playbook run on-box) that renders every
non-secret manifest for a named service and commits the YAML to the `deploy` branch. Verify by
diffing rendered output against what is live on the host — byte-identical is the acceptance
test, and §1's measurement says it should be.

**Slice 3 — two leaf services.** Selection criteria: no `secret*.j2`, no PVC, no CRD
dependency. `littlelink` and `bento-pdf` both qualify — each renders only
`deployment/ingressroute/service`. (`speedtest` looks like a candidate but carries a
`secret.yaml.j2`. Per §4 that is not disqualifying — a service can move while its Secret
manifest stays in `manifests_files` on the Ansible path — but keep slice 3 free of the
question.) Remove them from `containers_list`, delete their staged manifest dirs,
add a `Kustomization` each with `prune: true`, `wait: true`, `healthChecks`. Exercise it: push
an image bump, watch it reconcile without an Ansible run. Then break one deliberately and
watch what happens — that is the rollback question answered empirically rather than on paper.

**Slice 4 — the stabilisation check, ported.** Move the restart-delta soak into
`monitor-bridge` keyed on `lastAppliedRevision`. Do this *before* widening, because it is the
one gate that has caught a real fault the controller cannot see.

**Slice 5 — ordering.** A service with a real dependency (an IngressRoute behind traefik's
CRDs, or something behind seed-volume) expressed as `dependsOn`. This is where you find out
whether the graph is expressible.

**Slice 6 — widen, or stop.** Decide from slices 3-5 whether the remaining ~35 eligible
services are worth moving. Stopping here is a legitimate outcome: even a partial move retires
the prune cron and proves the drift detection.

---

## 7. The honest case for doing nothing

The current pipeline is not broken. It is well-tested (1522 lines of tests on `deploy_logic`
alone), and every gate in it exists because something bit. Flux's genuine wins — GC, drift
detection, parallel reconcile, declared ordering — are real, but three of the four are
addressable inside the existing design at lower cost than a migration.

What tips it, in my reading, is the *shape* of the machinery rather than any single defect:
`gitops_deploy.py` + `deploy_logic.py` + the denylist filter + the stale-denylist cross-check
are ~1,950 lines re-implementing pull-based reconciliation, and their most-documented failure
modes (config-on-host behind origin, path→service mapping, "which change is safe to
auto-deploy") are exactly the problems a controller does not have, because it holds no
per-host rendered state. Slices 1-3 cost little and answer the question with evidence.


---

## 8. Migrating off Jinja, role by role

**Yes, it slices, and the slice boundary is the role — the same boundary option 2 already uses.**
Flux's kustomize-controller generates a `kustomization.yaml` for any `spec.path` holding plain
Kubernetes manifests, so a directory of Ansible-rendered YAML and a directory of hand-written
Kustomize bases are both valid sources. Mixed mode costs nothing, which is what makes
incremental migration possible at all. A role that finishes the port needs no renderer, so Flux
points at the repo directly and the deploy branch stops existing for that role.

### What it buys

| Gain | Detail |
|---|---|
| **Offline schema validation** | There is none today. `validate_k8s_manifests.py` renders and parses YAML — it never checks a field name against a schema. `--dry-run` does, but it needs a live API server and refuses ~17 services. Native YAML means `kubeconform` in CI over every ported role, on a PR, with no cluster. This is a new capability, not a refactor. |
| **Renovate's built-in kustomize manager** | Image pins are found today by a custom regex on `^ansible/roles/k8s/[^/]+/defaults/main\.yml$`. A `kustomization.yaml` `images:` block is read by the manager Renovate ships and tests. One less bespoke regex to keep correct. |
| **`k8s_namespace` disappears** | 321 of 1,925 substitutions across all manifests are `k8s_namespace`. Kustomize's `namespace:` field sets it for every resource. A clean total win — one sixth of the templating is this one variable. |
| **`tz` / `puid` / `pgid` dedupe** | 119 more substitutions, identical in every role. One shared Kustomize component patching the env block replaces all of them, and it is genuinely better than today: a component is applied by reference, whereas the Jinja is copied per template. |

### What it costs

**The inventory coupling is the real price, not the template rewrite.** Manifests render inside
`deploy.yml`'s loop over `containers_list`, so `container_item.port` (151 uses) and
`container_item.hostname` are the *inventory entry*, not role variables. Kustomize has no
substitution, so a ported role hardcodes them. Two things read the whole list and would then be
reading a second copy: `roles/k8s/uptime-kuma/templates/deployment.yaml.j2` derives its monitors
from every entry with a `hostname`, and `roles/k8s/artifacts/templates/configmap.yaml.j2`
generates from the list too. Drift between a role's IngressRoute and its Kuma monitor becomes
possible where today it structurally cannot.

**`domain` is a regression.** 115 uses, and Kustomize's answer is either `replacements:` sourced
from a ConfigMap — verbose, and one block per manifest that mentions a hostname — or hardcoding
`daniel-hunter.com` into 30-odd IngressRoutes. Neither is as good as `{{ domain }}`.

**`lookup('template')` cannot be ported at all.** It means Jinja *inside* an app config file.
`configMapGenerator` can inline a file; it cannot template one.

### The tiers

Derived from the templates themselves — conditionals, loops, lookups, and whether the role's
`containers_list` entry carries a `hostname` or `port`:

| Tier | Count | Roles | Price |
|---|---|---|---|
| **A — port now** | 14 | `autofix-bridge` `cloudflare-ddns` `dri-device-plugin` `media-volume` `monitor-bridge` `mosquitto` `n8n-images` `node-exporter` `nut` `registry` `terraria` `terraria-stats` `valheim` `valheim-stats` | Pure `{{ var }}`, and the inventory entry carries neither `hostname` nor `port`. No second source of truth is created. |
| **B — port, accepting one duplicate** | 18 | `bazarr` `bento-pdf` `code-server` `homelab-mcp` `ical-proxy` `jellyfin` `littlelink` `longhorn-ui` `n8n` `peanut` `prowlarr` `qbittorrent` `radarr` `scrutiny` `sonarr` `speedtest` `tdarr` `wg-easy` | Also pure substitution, but the port or hostname now lives in two places. Needs a CI check asserting the manifest agrees with `containers_list`. |
| **C — needs Kustomize features** | 11 | `authelia` `claude-otel` `freshrss` `headlamp` `healthchecks` `karakeep` `loki-homelab` `pi-peer-backup` `pihole` `seed-volume` `traefik` | Conditionals, loops, or `lookup('file')`. The file lookups map cleanly onto `configMapGenerator`; the loops over static lists get unrolled. Judge one before committing to the tier. |
| **D — stays Jinja** | 11 | `artifacts` `configarr` `crowdsec` `home-assistant` `homepage` `image-builder` `janitorr` `livesync` `netpol-baseline` `uptime-kuma` `zigbee2mqtt` | Generators and templated app config. `netpol-baseline` holds 26 of the repo's 38 conditionals and 50 of its 121 filters across 29 files; `uptime-kuma` holds 131 substitutions of two variables. These are programs that emit manifests, not manifests. They should never be ported. |

Tier D is a third of the roles and most of the complexity, and naming it as permanent is what
makes the rest tractable. The realistic end state is not "no Jinja" — it is Ansible rendering
eleven generator roles while Flux reads the other forty-odd from the repo directly.

### The strongest win is separable — and that decides it

`scripts/validate_k8s_manifests.py` already renders every template with daniel-box's real
`containers_list` and parses the result (`docs = list(yaml.load_all(rendered, ...))`, line 230).
The rendered YAML is in hand, inside an existing loop. Adding schema validation there — a
kubeconform binary or an offline schema library — buys win #1 on all 52 roles now, with no
migration, no hardcoded ports, and no `domain` regression.

That removes the only new *capability* on the list. What the port still buys is tidiness:
`namespace:` instead of 321 substitutions, a shared component instead of 119, and a built-in
Renovate manager instead of a custom regex. Real, but not worth a multi-week rewrite that also
duplicates every port and hostname. **Recommendation: don't port. Add kubeconform to the
existing validator instead.** Port a tier-A role only if something else already forces its
manifests to be rewritten.

### Sequencing

Slice 7 is now one task, not a migration: teach `validate_k8s_manifests.py` to schema-check the
docs it already renders, and wire it into the existing prek hook. The Kustomize port stays
available and stays optional; nothing downstream depends on it.
