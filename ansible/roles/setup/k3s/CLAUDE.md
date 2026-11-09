# `setup/k3s` — emptied; the host packages own both k3s nodes

The role's `tasks/main.yml` is one unconditional `meta: end_role`, under a `# DECIDED` marker.
H7.44 emptied it. It stays until H7.20a, because `ansible/k3s-bringup.yml` names it and an edit
to that playbook parks the deployer. Repo-root `CLAUDE.md` has the conventions.

`tasks/agent.yml`, `tasks/agent_verify.yml` and `tasks/static-address.yml` are each one task that
refuses, because `k3s-bringup.yml`'s join and pin plays import them by name until H7.20a. A
silent end would read as a finished join or pin.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `k3s-bringup.yml --tags "k3s"`; `k3s-bringup.yml --tags "k3s_agent"`;
  `k3s-bringup.yml --tags "node-address"`; ends on hosts where `True` (leading `meta:
  end_role`)
- **Crons / timers:** none (the role ends on every host before its first task)
<!-- /generated_from -->

## Where the work went

- **The server's host half** is `homelab-daniel-box`'s: `hosts/components/k3s-server/` holds the
  flags, the same-path files, the CoreDNS `.skip` marker, the join rules and the address pin.
  `hosts/components/k3s-host-dns/` holds the resolver and `hosts/components/k3s-host-checks/` the
  backup and disk checks.
- **The agent's host half** is `homelab-daniel-server`'s, in `hosts/components/k3s-agent/`.
- **The install and the post-start checks** are `hosts/bootstrap-host.sh` and
  `docs/host-packages.md`'s *Rebuilding daniel-box* and *Rebuilding daniel-server*: the
  installer, the Docker refusal, the secrets-encryption status and `rotate-keys`, the
  registered InternalIP, the embedded-etcd datastore, the bundled-addon check and the CoreDNS
  marker order.
- **The storage smoke test** is `scripts/diagnostics/storage_smoke.py`.
- **The k3s pin** is the `system-upgrade` role's (`ansible/roles/k8s/system-upgrade/defaults/main.yml`),
  and SUC's Plans are the only writers of each node's version.

`defaults/main.yml` and `templates/` stay, because both components render through `role: k3s`
and read those keys and the netplan template.

## Rollback

A revert of H7.44 restores the role's tasks. The operator then re-applies the role on
daniel-box: `cd /home/ubuntu/server && uv run ansible-playbook ansible/k3s-bringup.yml --tags k3s`
(operator-only). That run reaches the installer only on a host with no k3s binary, and
`rotate-keys` only on a cluster whose Secrets have not finished re-encrypting.
