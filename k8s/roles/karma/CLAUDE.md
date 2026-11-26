# karma — the alert UI over Alertmanager

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=karma` gates the rollout
- **Image:** `ghcr.io/prymitive/karma` (`karma_k8s_image`)
- **Route:** `karma.<domain>` · `karma.local.<domain>`, Authelia two_factor
- **Claims:** none (no PVC)
<!-- /generated_from -->

## Notable

- **Karma replaces Alertmanager's own UI**, which has no route of its own.
  `docs/archive/monitoring/alertmanager-ui-and-readers.md` records why. Karma
  reads Alertmanager in-cluster, and `proxy: true` sends the silence writes its UI makes
  through the Karma pod, because a browser cannot reach the in-cluster URI.
  `k8s/roles/netpol-baseline/templates/networkpolicy-alertmanager.yaml.j2` admits it.
- **Both routes need `admins` at `two_factor`.** Karma can silence every page. The authelia
  role's hand-written rule pairs for `karma.local` and `karma.<domain>` admit `group:admins` and
  deny everyone else. The `karma.local` pair admits RFC1918 sources only. The public pair admits
  any source, since a Cloudflare request carries no LAN address. Without the public `deny`, a
  non-admin would fall through to the `*.<domain>` two_factor wildcard, which has no subject.
  The routes carry no `ClientIP` fence, because blackbox-exporter's ingress probe arrives from a
  pod address and a fence answers it 404.
  `tests/k8s/edge/test_alertmanager_read_route.py::test_karma_names_authelia_on_both_its_lan_and_public_routes`
  holds the routes, and
  `tests/services/authelia/test_authelia_access_tiers.py::test_a_user_outside_admins_is_denied_a_silence_ui`
  and `::test_the_public_silence_ui_admits_admins_only_from_any_source` hold the rules.
- **Severity colours and sort order come from `karma_severities` in `defaults/main.yml`.**
  `k8s/roles/karma/tests/test_karma_config.py::test_every_rule_severity_has_a_colour_and_a_rank`
  fails when an alert rule uses a severity value the list does not carry.
- **Karma authenticates by the `Remote-User` header** that Authelia's forwardAuth sets, so a
  silence records who made it. `/health` bypasses that check, and both probes use it.
- **The default filters hide resolved alerts and the `blackhole` receiver** (`shadow` and
  `drill` alerts). The Watchdog is a healthcheck filter: Karma hides it, and shows an error
  when it stops firing.

## Editing

- Manifests: `templates/`. The Service renders from `k8s/templates/service-default.yaml.j2`;
  the IngressRoute is the role's own.
- Karma's config: `templates/config/karma.yaml.j2`, embedded in `templates/configmap.yaml.j2`.
  The renderer's restart annotation rolls the pod on a change.
- Image pin, Renovate stance, resources, uid and severities: `defaults/main.yml`.
