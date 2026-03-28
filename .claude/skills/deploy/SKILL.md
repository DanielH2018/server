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
3. If dry run: run `ansible-playbook ansible/deploy.yml --tags "<service>" --check`
4. If dry run passes or they skip it: run `ansible-playbook ansible/deploy.yml --tags "<service>"`
5. Report the result

Run all commands from `/home/ubuntu/server`.
