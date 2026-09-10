# deploy-ui — the route half of deploy.local

Route-only role. The backend is `deploy-ui.service` on daniel-box (`roles/setup/deploy_ui`),
reached through a selector-less Service whose EndpointSlice names `k8s_node_client_ip`. This is the
repo's first Traefik→host route. Two facts follow:

- `probe.py health deploy-ui` finds no workload and skips. The verification is the page
  loading at `deploy.local.<domain>` behind two-factor.
- The route works only while ufw on daniel-box admits the port from the cluster
  (`deploy_ui_allowed_sources` in the setup role). A 504 from Traefik with the unit active
  is that rule, not the daemon.

Deploy the route with `./scripts/deploy.sh --tags "deploy-ui"`; the daemon it fronts is a
separate tag on another playbook — `uv run ansible-playbook ansible/initial_setup.yml
--tags deploy_ui`, which `deploy.sh` does not carry.

Authelia policy: `two_factor`, ENFORCED by `ansible/tests/k8s/test_deploy_ui_is_two_factor.py`.
Kept out of `STAGING_SUBSET`: daniel-stage has no daemon to route to.
