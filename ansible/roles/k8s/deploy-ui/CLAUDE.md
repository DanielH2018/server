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
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=deploy-ui` with exit 5
- **Route:** `deploy.local.<domain>` (LAN only), Authelia two_factor
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible** (D5.6, the second Wave A PR). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. To
  give it back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain
  revert of the flag leaves two writers on the objects.
- **Where the task went.** `tasks/main.yml` holds one task, the include of `k8s/manifests`,
  and no Flux path runs it. It stays for the handback. `manifests_rollout: ''` has nothing to
  replace: there is no workload to roll, so the Kustomization's `wait` checks only that the
  Service, EndpointSlice and IngressRoute applied.
- **The EndpointSlice address is baked in at render time.** The renderer writes
  `k8s_node_client_ip` into the bundle, so a change to that variable reaches the cluster only
  with the next promotion that publishes this tag.
- **A bad route change exposes a page that starts deploys and clears the deployer's hold.**
  Under Ansible a route change reached the cluster only through a hand deploy. Under Flux
  every merged change applies on the next promotion, so review is the gate.
  `k8s_autodeploy: false` stays in `defaults/main.yml` with its reason, but it gates nothing
  here: the deployer skips the tag, and the role has no image for Renovate to automerge.

The daemon the route fronts is a separate tag on another playbook, `uv run ansible-playbook
ansible/initial_setup.yml --tags deploy_ui`, and Ansible still applies it.

Authelia policy: `auth_tier: two_factor` on the containers_list entry, which the authelia role
renders into its access_control rules. ENFORCED for every SSO entry by
`ansible/tests/services/test_authelia_access_tiers.py`.
