# pihole — LAN DNS resolver, running two instances

Pi-hole plus an unbound sidecar, deployed as two independent pods for deploy-time DNS
continuity. Coexisted with a Docker-era copy through the DNS cutover; that copy is retired.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "pihole"`
- **Images:** `pihole/pihole` (`pihole_k8s_image`), `klutchell/unbound`
  (`pihole_k8s_unbound_image`)
- **Route:** `pihole.<domain>` · `pihole.local.<domain>`, Authelia one_factor
- **Claims:** `pihole-etc` (no backup (StorageClass longhorn-nobackup)), `pihole-etc-2` (no
  backup (StorageClass longhorn-nobackup)), plus the claims a template loop declares (`{{ claim
  }}`)
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — platform — LAN DNS resolver; a failed
  deploy breaks name resolution fleet-wide, and host probes stay green through that kind of
  outage
<!-- /generated_from -->

- **The unbound sidecar** listens on `pihole_k8s_unbound_port` inside the pod; FTL owns `:53`
  in the shared network namespace.
- **The route is the web UI only** (port 80). LAN DNS itself is served on
  `pihole_k8s_lan_ip` (`dns_k8s_vip`), not through Traefik.
- **Persists:** two independent PVCs, `pihole_k8s_claim` / `pihole_k8s_claim_2`, one per pod
  — `/etc/pihole` is RWO/single-writer, so each instance gets its own rather than sharing.
  `longhorn-nobackup`: every byte is derivable — `gravity.db` from `pihole_k8s_adlists`,
  `pihole.toml` from the `FTLCONF` env, `pihole-FTL.db` is query history, not state.

## Notable
- **Blocklist state is declared, not volume-seeded.** `tasks/main.yml` reconciles
  `pihole_k8s_adlists`/`pihole_k8s_regex_deny` into `gravity.db` with idempotent
  INSERT/UPDATE, then rebuilds gravity only on a change — `pihole-FTL.db` moves on every DNS
  query, so a coexistence seed could never pass a quiescent-state verification.
- **Both the apply and the restart are sequenced by hand, not left to the shared batch drain.**
  The two pods are restarted one at a time (`manifests_rollout: ''` plus `tasks/roll_one.yml`)
  so a rollout never takes both Pi-holes down together, which is the whole reason a second
  instance exists.
- **Instance 2's Deployment is applied separately, from `/etc/rancher/k3s/manifests/pihole-instance-2/`.**
  Sequencing the restarts was not enough: `kubectl apply -f <dir>/` applies every file in a
  directory in one request, so while both Deployments rendered into one `deployment.yaml` an
  image-pin bump changed both pod templates in the same second and the controller Recreate-cycled
  both instances before any restart task ran (2026-09-28, issue #2884 — 52s of LAN DNS
  downtime, and both new pods failed their image pull against the resolver they had just
  replaced). `templates/deployment.yaml.j2` now carries instance 1 and
  `templates/deployment-2.yaml.j2` instance 2, both from one macro body in
  `templates/pihole-deployment.yaml.j2`, and `tasks/apply_instance_2.yml` applies the second only
  after `roll_one.yml` has proved the first is serving.
- **The shared role still renders instance 2, and still digests it; only the apply is this
  role's.** `manifests_deferred_files: [deployment-2.yaml]` with
  `manifests_deferred_dir_name: pihole-instance-2` is that contract (#2899). Rendering it here
  instead left its bytes outside `manifests_digest`, so a change to `deployment-2.yaml.j2` moved
  no digest at all and `probe.py releases --stale-only` fell back to paths and diffs for half
  this role's workload. One cost remains, deliberately: a dry run renders instance 2's manifest
  but never shows it to the API server, because `kubectl apply -f <dir>/` is not recursive and a
  dry run must not write the node outside `roles/k8s/manifests` (#2611/#2614). The macro keeps
  that gap small — instance 1's dry run exercises the schema instance 2 renders.
- **`probe.py health pihole` holds a roll expectation for pihole-2 only because `roll_one.yml`
  writes one.** `rolled_by_role: true` on the `manifests_self_rollouts` entry records
  `restart: false` and hands the decision to `roll_one.yml`, which raises it through
  `tasks/rollout_amend.yml` only where it issued a `rollout restart` (#2902). Deploy time is
  covered either way: `roll_one.yml` blocks on pihole-2's `rollout status`, and `Verify both
  Pi-hole instances have a ready DNS endpoint` refuses fewer than two ready endpoints.
- **Each private restart needs the render AND the matching apply's own `changed` (#3127,
  mirroring #3115).** `manifests_render is changed` compares rendered bytes alone, so on its own
  it rolled the LAN resolvers for a YAML-comment edit. `roll_one.yml` now pairs each render
  check with the apply that carries those bytes. The per-trigger derivation is in
  `docs/pihole-dns-continuity-record.md`.
- **`roll_one.yml` refuses a sibling that is terminating.** A pod keeps phase `Running` and
  condition `Ready` for its whole grace period, so `kubectl wait` alone reported a
  half-gone sibling as a serving one. The check reads `deletionTimestamp` and fails the play.
- **LAN reverse-DNS is forwarded to the router, with `server=` rather than `rev-server=`.**
  `templates/config/pihole-dnsmasq.conf.j2` forwards the zone `lan_subnet` implies to
  `lan_router_ip`, so PTR lookups for DHCP clients return the names the router leased them.
  Pi-hole's own `rev-server=` is deliberately unused: it is a Pi-hole-only directive FTL's
  parser could reject at startup — on both instances at once, leaving no resolver to fetch a
  fix through — and it also forwards a whole TLD, which would hand the `.lan` names the
  `host-record=` lines answer for to the router. ENFORCED by
  `ansible/tests/services/test_pihole_lan_reverse_forwarding.py`.
- **Rotating `pihole_password` leaves pihole-exporter on the old value.** The exporter reads
  the password from this role's `pihole-env` Secret, and it is flux-owned, so neither this
  role's restarts nor `deploy.sh` roll it, and `up{job="pihole"}` stays at 0. After the
  rotation the operator rolls it by hand, as `ansible/roles/k8s/pihole-exporter/CLAUDE.md`
  says. `secret_rotation.py consumers pihole_password` does not list it yet (#5416).
- **`pihole_k8s_dns_cluster_ip` is a pinned ClusterIP**, immutable once bound — cluster DNS
  forwards there rather than to the LAN VIP, since the VIP is subject to
  `externalTrafficPolicy: Local`. Recreating the Service needs a new address chosen deliberately.

## The Flux forms wait in `templates/verify-inert/`

D5 step 26 converts this role before its Wave D flip. The conversion is inert: Ansible still
deploys pihole, and nothing renders `templates/verify-inert/`. The flip PR renames it to
`templates/verify/`, which makes the renderer emit `pihole-verify`, and deletes the tasks the
forms below replace.

- **The gravity reconcile is `gravity.yaml.j2`, a mutation Job.** It runs
  `files/pihole_gravity.py` against each FTL's v6 REST API, through the `pihole` and
  `pihole-2-web` Services, with the password from `pihole-env`. So it needs no `pods/exec` and
  no Role. It adds what is absent, sets `enabled` where it differs, reads the result back, and
  rebuilds gravity on both instances only after a change. FTL validates a regex before storing
  it, so an invalid pattern in `pihole_k8s_regex_deny` fails the Job rather than sitting unused.
  netpol-baseline's `networkpolicy-pihole.yaml.j2` admits its label, `app: pihole-gravity`, on
  port 80.
- **`roll_one.yml`'s one-at-a-time roll becomes Kustomization order.** Instance 1 renders in
  `pihole`, instance 2 in `pihole-deferred`, and `pihole-deferred` depends on `pihole` with
  `wait: true`. A shared ConfigMap edit moves the restart annotation the renderer stamps on
  both instances, and instance 2 rolls only after instance 1 is Ready. `pihole-verify` depends on `pihole-deferred`, so the
  reconcile meets two Ready instances.
- **The ready-endpoint check is deleted at the flip, with no successor.** `Verify both Pi-hole
  instances have a ready DNS endpoint` counts the Ready pods behind `pihole-dns`. Under Flux,
  `wait: true` on `pihole` holds instance 1 Ready and `wait: true` on `pihole-deferred` holds
  instance 2 Ready. `pihole-dns` selects both by `app: pihole`, so two Ready Deployments are
  two ready endpoints.
- **The pinned-VIP assert becomes observability's `PiholeLanAddressNotPinned` rule.** The rule
  reads `pihole-dns`'s LoadBalancer ingress from kube-state-metrics, at `warning` with
  `verify="true"`, so the soak checks it on every revision. It lands while observability is
  Ansible-owned, so the flip PR deletes the two LAN-address tasks without touching
  observability. Its address is a literal, so moving `dns_k8s_vip` is an edit to that rule
  too. `ansible/tests/services/test_lan_address_rules_agree.py` holds the literal to the
  rendered Service.
- **One check has no Flux form yet.** `roll_one.yml` refuses to roll an instance while its
  sibling is not Ready or is terminating. Under Flux, `pihole` rolls instance 1 whatever state
  instance 2 is in, and `pihole` cannot depend on `pihole-deferred` without a cycle (#5695).
  `tests/test_pihole_gravity.py` covers the Job and the ordering.

## Editing
Adlists/regex: `defaults/main.yml`.
