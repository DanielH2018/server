---
name: deploy
description: Deploy a container service using Ansible. Use when the user wants to deploy or redeploy a specific service.
allowed-tools: Bash, Glob
---

Deploy a container service using Ansible.

If the user provided a service name as an argument, use it directly. Otherwise ask which service to deploy.

Steps:
1. Confirm the service name matches a role in `ansible/roles/containers/`
2. Ask if they want a dry run first (`--check` mode)
3. If dry run: run `uv run ansible-playbook ansible/deploy.yml --tags "<service>" --check`
4. If dry run passes or they skip it: run `uv run ansible-playbook ansible/deploy.yml --tags "<service>"`
5. **Verify the container actually came up healthy** — Ansible reporting `ok`/`changed`
   only means the playbook ran, not that the container is up (it can deploy cleanly then
   crash-loop or fail its healthcheck). Gate on:
   `uv run python scripts/probe.py health <service>` — exit 0 = running + healthy. This is
   allow-listed, so it runs without a prompt.
   - The container name usually equals the service/role name. If `health` reports
     `not found`, find the real name with `docker ps` and re-run.
   - `probe.py health` inspects the **local** (server) Docker daemon, so it only applies to
     `daniel-server` services — skip it for Pi deploys (verify those via the Pi's Uptime Kuma
     monitor instead).
   - For a config-only run (`--skip-tags deploy`), the container isn't recreated, so this is
     just a liveness check, not a deploy verification.
6. Report the result, including the health line. If the gate fails (non-zero exit / unhealthy),
   surface the last healthcheck line it prints and pull recent logs
   (`docker logs --tail 50 <service>` or `uv run python scripts/probe.py loki-query '{container="<service>"}'`)
   before declaring success.

Run all commands from `/home/ubuntu/server`. Always go through `uv run` — bare
`ansible-playbook` (the uv-tool shim) lacks the `community.docker` module deps and fails.
For a service on the Pi, add `-e target=daniel-pi` (deploy.yml defaults `hosts:` to the
local hostname — `--limit` alone matches nothing).
