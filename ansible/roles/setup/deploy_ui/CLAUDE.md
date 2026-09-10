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
  The `roles/k8s/deploy-ui` route role ships the guard that pins that.

DECIDED: a pod inside the cluster reaches the daemon without Authelia. Accepted 2026-09-10 —
a hostile pod already has kubectl-adjacent reach, and netpol-baseline fences pod egress.
Every POST also needs `X-Deploy-UI: 1` so a cross-site form cannot ride the Authelia cookie.

## Writes

Each write spawns the command detached, logs to `~/.local/state/deploy-ui/`, and emits one
logfmt line via `logger -t deploy-ui`. Land and deploy refuse under `hold_sha`; the hold
clears only as the `hold_sha` + `hold_plane` pair against a SHA the operator typed.

## Deploy

`uv run ansible-playbook ansible/initial_setup.yml --tags deploy_ui` (a setup-plane role;
`deploy.sh` does not carry it). Verify: `curl -s http://10.0.0.215:8790/api/state` from the
box.
