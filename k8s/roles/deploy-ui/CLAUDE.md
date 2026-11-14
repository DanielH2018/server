# deploy-ui — the route half of deploy.local

The route, plus the identity the Games panel scales the game servers with. The backend is `deploy-ui.service` on daniel-box (`hosts/components/deploy-ui/`),
reached through a selector-less Service whose EndpointSlice names `k8s_node_client_ip`. This is the
repo's first Traefik→host route. Two facts follow:

- `probe.py health deploy-ui` finds no workload and skips. The verification is the page
  loading at `deploy.local.<domain>` behind two-factor.
- The route works only while ufw on daniel-box admits the port from the cluster
  (the `ufw:` rules in `hosts/components/deploy-ui/component.yml`). A 504 from Traefik with
  the unit active is that rule, not the daemon.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=deploy-ui` gates the rollout
- **Route:** `deploy.local.<domain>` (LAN only), Authelia two_factor
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it.** Its `containers_list` entry
  is in `cluster/services.yml`, so nothing applies it by hand, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle.
- **Its checks.** Its `manifests_rollout: ''` default names no workload, because the role
  renders none. The Kustomization's `wait` checks only that the Service, EndpointSlice,
  IngressRoute and the games identity's objects applied.
- **The EndpointSlice address is baked in at render time.** The renderer writes
  `k8s_node_client_ip` into the bundle, so a change to that variable reaches the cluster only
  with the next promotion that publishes this tag.
- **A bad route change exposes a page that starts and cancels landings.**
  Flux applies every merged change on the next promotion, so review is the gate.
  `k8s_autodeploy: false` stays in `defaults/main.yml` with its reason, but it gates nothing
  here: the flag only holds back Renovate's automerge, and the role has no image for Renovate
  to bump.

## The games identity

`templates/games-rbac.yaml.j2` renders `homelab-games` (`deploy_ui_games_sa_name`) in the
workload namespace: a ServiceAccount, its token Secret, and a Role that grants `get`, `patch`
and `update` on `deployments/scale` for the Deployments `game_servers` lists, and nothing else.
The Games panel starts and stops the game servers with it, under ADR-0033.

- **It is the second write path to the cluster, and the Role is its bound.** The
  `games-identity-only-scales-the-game-deployments` row in `tests/k8s/_rbac_property_rows.py`
  holds every rendered Role named for the identity and every binding that names it to that
  rule.
- **It lives here, not in cluster-base.** A rebuilt cluster applies cluster-base at step 6 of
  the DR order, before anything creates `homelab`, and a `homelab` object there would stall the
  bring-up (`tests/k8s/test_step6_tags_render_into_created_namespaces.py`).
- **The operator mints the host's copy of the token.** It reads the token Secret, stores it in
  daniel-box's host secrets, and deploy-ui loads it with `LoadCredential=` (**operator-only**).
  Deleting the Secret revokes the token.

The daemon the route fronts ships in daniel-box's host package (`hosts/components/deploy-ui/`), which reaches the host when the builder publishes the next `homelab-daniel-box`.

Authelia policy: `auth_tier: two_factor` on the containers_list entry, which the authelia role
renders into its access_control rules. ENFORCED for every SSO entry by
`tests/services/test_authelia_access_tiers.py`.
