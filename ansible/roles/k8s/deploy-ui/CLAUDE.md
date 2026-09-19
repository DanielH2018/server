# deploy-ui — the route half of deploy.local

Route-only role. The backend is `deploy-ui.service` on daniel-box (`roles/setup/deploy_ui`),
reached through a selector-less Service whose EndpointSlice names `k8s_node_client_ip`. This is the
repo's first Traefik→host route. Two facts follow:

- `probe.py health deploy-ui` finds no workload and skips. The verification is the page
  loading at `deploy.local.<domain>` behind two-factor.
- The route works only while ufw on daniel-box admits the port from the cluster
  (`deploy_ui_allowed_sources` in the setup role). A 504 from Traefik with the unit active
  is that rule, not the daemon.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "deploy-ui"`
- **Route:** `deploy.local.<domain>` (LAN only), Authelia two_factor
- **Claims:** none (no PVC)
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — renders no Deployment — route-only
  (Service + EndpointSlice + IngressRoute) in front of deploy-ui.service on daniel-box; a bad
  route change exposes a page that starts deploys and clears the deployer's hold
<!-- /generated_from -->

Deploy the route with `./scripts/deploy.sh --tags "deploy-ui"`; the daemon it fronts is a
separate tag on another playbook — `uv run ansible-playbook ansible/initial_setup.yml
--tags deploy_ui`, which `deploy.sh` does not carry.

Authelia policy: `auth_tier: two_factor` on the containers_list entry, which the authelia role
renders into its access_control rules. ENFORCED for every SSO entry by
`ansible/tests/services/test_authelia_access_tiers.py`.
Kept out of `STAGING_SUBSET`: daniel-stage has no daemon to route to.
