# deploy_ui — the host half of deploy.local

`deploy-ui.service` on the GitOps host (`has_gitops`), `User={{ sys_user }}`, cwd the primary
checkout. It serves the page and API `roles/k8s/deploy-ui` routes to, and runs `land.sh`,
`deploy.sh`, `probe.py releases` and `gh` exactly as the operator would from a terminal.

## Trust

The daemon does no authentication. Two gates stand in front of it; removing either is a
visible decision:

- ufw admits `deploy_ui_port` from `deploy_ui_allowed_sources` only (the pod CIDR and every
  node IP, because flannel masquerades pod→LAN traffic to the sending node).
  ENFORCED: `ansible/tests/setup/test_deploy_ui_firewall_sources.py`.
- Authelia `two_factor` gates `deploy.local` (`roles/k8s/authelia/templates/config-secret.yaml.j2`).
  ENFORCED: `ansible/tests/k8s/test_deploy_ui_is_two_factor.py`.
  The `roles/k8s/deploy-ui` route role ships the guard that pins that.

DECIDED: a pod inside the cluster reaches the daemon without Authelia. Accepted 2026-09-10 —
a hostile pod already has kubectl-adjacent reach. Nothing stops it at the network layer:
netpol-baseline is INGRESS ONLY (`roles/k8s/netpol-baseline/templates/networkpolicy.yaml.j2`),
so pod egress is unfenced on this cluster.
Every POST also needs `X-Deploy-UI: 1` so a cross-site form cannot ride the Authelia cookie.

## Turning it off

- Stop and disable the daemon on daniel-box: `systemctl disable --now deploy-ui.service`.
- Remove the `deploy-ui` entry from `containers_list` in `host_vars/daniel-box.yml` and
  deploy, which drops the route (`roles/k8s/deploy-ui`) and `deploy.local`.
- The ufw task only ADDS rules, one per `deploy_ui_allowed_sources` entry. A source removed
  from that list keeps its rule until someone deletes it by hand:
  `ufw delete allow proto tcp from <src> to any port 8790`.

## Writes

Each write spawns the command detached, logs to `~/.local/state/deploy-ui/`, and emits one
logfmt line via `logger -t deploy-ui`. Land and deploy refuse under `hold_sha`; the hold
clears only as the `hold_sha` + `hold_plane` pair against a SHA the operator typed.

`/api/deploy` takes one SERVICE tag. `deploy.sh --list-services` also prints the block tags
(`config`, `deploy`, `cron`) and Ansible's `always`, each of which selects every container
role at once, so `writes.NON_SERVICE_TAGS` subtracts them from the allowlist and the guard
refuses one with a 409. A test asserts that literal equals `BLOCK_TAGS | RESERVED_TAGS` in
`scripts/deploy_tools/deploy_tags.py`, because the daemon runs outside the repo venv and
cannot import it.

Log names carry a `mkstemp` suffix as well as the second-granular timestamp. Two writes in
one second used to name the same file, and the second open truncated a log the first process
still held.

## Deploy

`uv run ansible-playbook ansible/initial_setup.yml --tags deploy_ui` (a setup-plane role;
`deploy.sh` does not carry it). Verify: `curl -s http://10.0.0.215:8790/api/state` from the
box.
