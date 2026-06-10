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
5. Report the result

Run all commands from `/home/ubuntu/server`. Always go through `uv run` — bare
`ansible-playbook` (the uv-tool shim) lacks the `community.docker` module deps and fails.
For a service on the Pi, add `-e target=daniel-pi` (deploy.yml defaults `hosts:` to the
local hostname — `--limit` alone matches nothing).
