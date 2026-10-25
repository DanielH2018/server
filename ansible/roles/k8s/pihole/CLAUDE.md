# pihole — LAN DNS resolver, running two instances

Pi-hole plus an unbound sidecar, deployed as two independent pods for deploy-time DNS
continuity. Coexisted with a Docker-era copy through the DNS cutover; that copy is retired.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses `--tags=pihole`
  with exit 5
- **Images:** `pihole/pihole` (`pihole_k8s_image`), `klutchell/unbound`
  (`pihole_k8s_unbound_image`)
- **Route:** `pihole.<domain>` · `pihole.local.<domain>`, Authelia one_factor
- **Claims:** `pihole-etc` (no backup (StorageClass longhorn-nobackup)), `pihole-etc-2` (no
  backup (StorageClass longhorn-nobackup)), plus the claims a template loop declares (`{{ claim
  }}`)
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
- **Blocklist state is declared, not volume-seeded.** `pihole_k8s_adlists` and
  `pihole_k8s_regex_deny` are the declaration, and `templates/verify/gravity.yaml.j2` reconciles
  them into each FTL. `pihole-FTL.db` moves on every DNS query, so a coexistence seed could never
  pass a quiescent-state verification.
- **The two Deployments render in separate Kustomizations on purpose.** Instance 1 is
  `templates/deployment.yaml.j2` in `pihole`, and instance 2 is `templates/deployment-2.yaml.j2`
  in `pihole-deferred`, both from one macro body in `templates/pihole-deployment.yaml.j2`. With
  both in one directory an image-pin bump changed both pod templates in the same second and the
  controller Recreate-cycled both instances (2026-09-28, #2884: 52s of LAN DNS downtime, and both
  new pods failed their image pull against the resolver they had just replaced).
  `manifests_deferred_files: [deployment-2.yaml]` with `manifests_deferred_dir_name:
  pihole-instance-2` is the contract (#2899). Why the Ansible-era roll was sequenced by hand is
  in `docs/pihole-dns-continuity-record.md`.
- **LAN reverse-DNS is forwarded to the router, with `server=` rather than `rev-server=`.**
  `templates/config/pihole-dnsmasq.conf.j2` forwards the zone `lan_subnet` implies to
  `lan_router_ip`, so PTR lookups for DHCP clients return the names the router leased them.
  Pi-hole's own `rev-server=` is deliberately unused: it is a Pi-hole-only directive FTL's
  parser could reject at startup — on both instances at once, leaving no resolver to fetch a
  fix through — and it also forwards a whole TLD, which would hand the `.lan` names the
  `host-record=` lines answer for to the router. ENFORCED by
  `ansible/tests/services/test_pihole_lan_reverse_forwarding.py`.
- **pihole-exporter reads `pihole_password` from this role's `pihole-env` Secret.** Its
  include declares the Secret under `manifests_outside_secrets`, so a rotation moves the
  exporter's restart checksum annotation and Flux rolls it with this role's pods (#5416). Renaming
  `pihole-env`, or moving the password to another key, needs the same edit in that
  declaration; `ansible/roles/k8s/pihole-exporter/CLAUDE.md` names the test that refuses a
  drift.
- **`pihole_k8s_dns_cluster_ip` is a pinned ClusterIP**, immutable once bound — cluster DNS
  forwards there rather than to the LAN VIP, since the VIP is subject to
  `externalTrafficPolicy: Local`. Recreating the Service needs a new address chosen deliberately.

## Flux applies it

- **Flux applies pihole, not Ansible** (D5, pihole's own window). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. To
  give it back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`. A plain
  revert of the flag leaves two writers on the objects, and the handback restores `roll_one.yml`,
  `apply_instance_2.yml` and the gravity tasks from the commit before the flip.
- **Where the tasks went.** `tasks/main.yml` holds the include of `k8s/manifests` alone, and no
  Flux path runs it. The include keeps `manifests_self_rollouts` because the renderer reads it to
  stamp the restart annotation on both instances.
- **The gravity reconcile is `templates/verify/gravity.yaml.j2`, a mutation Job.** It runs
  `files/pihole_gravity.py` against each FTL's v6 REST API, through the `pihole` and
  `pihole-2-web` Services, with the password from `pihole-env`. So it needs no `pods/exec` and
  no Role. It adds what is absent, sets `enabled` where it differs, reads the result back, and
  rebuilds gravity on both instances only after a change. FTL validates a regex before storing
  it, so an invalid pattern in `pihole_k8s_regex_deny` fails the Job rather than sitting unused.
  netpol-baseline's `networkpolicy-pihole.yaml.j2` admits its label, `app: pihole-gravity`, on
  port 80.
- **`roll_one.yml`'s one-at-a-time roll is Kustomization order.** Instance 1 renders in
  `pihole`, instance 2 in `pihole-deferred`, and `pihole-deferred` depends on `pihole` with
  `wait: true`. A shared ConfigMap edit moves the restart annotation the renderer stamps on
  both instances, and instance 2 rolls only after instance 1 is Ready. `pihole-verify` depends
  on `pihole-deferred`, so the reconcile meets two Ready instances.
  `tests/test_pihole_gravity.py` pins that order.
- **The ready-endpoint check has no successor.** `wait: true` on `pihole` holds instance 1
  Ready and `wait: true` on `pihole-deferred` holds instance 2 Ready. `pihole-dns` selects both
  by `app: pihole`, so two Ready Deployments are two ready endpoints.
- **The pinned-VIP assert is observability's `PiholeLanAddressNotPinned` rule.** The rule
  reads `pihole-dns`'s LoadBalancer ingress from kube-state-metrics, at `warning` with
  `verify="true"`, so the soak checks it on every revision. Its address is a literal, so moving
  `dns_k8s_vip` is an edit to that rule too.
  `ansible/tests/services/test_lan_address_rules_agree.py` holds the literal to the rendered
  Service.

## The sibling guard and its break-glass

- **`roll_one.yml`'s sibling check is `templates/precheck/guard.yaml.j2`.** Under Flux,
  `pihole` rolls instance 1 whatever state instance 2 is in, and `pihole` cannot depend on
  `pihole-deferred` without a cycle (#5695). The renderer writes `templates/precheck/` to
  `pihole-precheck`, which `pihole` depends on (#5719). `files/pihole_sibling_guard.py` reads the
  EndpointSlices of `pihole` and `pihole-2-web`, which each select one instance. It fails while
  pihole-2 has no ready, non-terminating endpoint for 300s, so Flux holds `pihole` and instance 1
  keeps serving. It passes at once when `pihole` has no EndpointSlice at all, so a first apply
  cannot deadlock on an instance 2 that only `pihole-deferred` creates. An API read that gets no
  answer, a 429 or a 5xx is retried inside the 300s wait. Its Role grants `get` and `list` on
  EndpointSlices in pihole's namespace.
- **The guard re-runs only on a revision that rolls instance 1.** Its name embeds a digest of
  instance 1's parsed Deployment, the shared ConfigMap and the Secret template, so a change to
  `deployment-2.yaml.j2` alone keeps the name and reaches pihole-2 past a passed guard.
- **A failed guard also holds `pihole-deferred`**, which depends on `pihole`. A revision that
  rolls instance 1 and would also fix a down pihole-2 cannot reach it while the guard fails.
  Deleting the failed Job does not help, because Flux recreates it under the same name.
- **Break-glass: set `pihole_k8s_sibling_guard: false` in `defaults/main.yml` (#5740).** The
  render then holds no guard ConfigMap and no guard Job, `pihole-precheck` keeps only the guard's
  RBAC, and Flux prunes the failed Job. Merge that change, wait for the revision to reach
  pihole-2 and for pihole-2 to be Ready, then restore `true` in a second PR. The restore renders
  the same Job name as before the bypass, and the pruned Job is gone, so the guard runs fresh and
  passes against a serving pihole-2. Flux takes no `-e`, so the switch is a git revision, and the
  two PRs record when the guard was off. `tests/test_pihole_precheck.py` pins both renders.

## Editing
Adlists/regex: `defaults/main.yml`.
