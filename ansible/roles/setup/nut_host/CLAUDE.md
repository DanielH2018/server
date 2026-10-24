# nut_host — emptied; the host packages own the UPS shutdown chain's host half

The role's tasks are one unconditional `meta: end_role`, under a `# DECIDED` marker in
`ansible/roles/setup/nut_host/tasks/main.yml`. H7.33 emptied it. It stays until H7.20a, because
`ansible/initial_setup.yml` names it and an edit to that playbook parks the deployer. Repo-root
`CLAUDE.md` has the conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "nut_host"` when `inventory_hostname == ups_host or
  nut_host_secondary_armed | bool`; ends on hosts where `True` (leading `meta: end_role`)
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
<!-- /generated_from -->

## Where the work went
`hosts/components/nut-secondary/` owns the secondary `upsmon` on both cluster nodes: the
`nut-monitor` drop-in that points upsmon at the rendered
`/var/lib/homelab-secrets/etc/nut/upsmon.conf`, the `homelab-ups-watchdog` timer, and
the APC udev rule, which matches nothing on daniel-box. `ansible/roles/k8s/nut/CLAUDE.md` owns the chain end to end.

- daniel-server's package is `homelab-daniel-server`, and its MONITOR line names `127.0.0.1`.
- daniel-box's package is `homelab-daniel-box`, and its MONITOR line names the `nut` Service's
  pinned ClusterIP (`nut_k8s_cluster_ip` in `ansible/roles/k8s/nut/defaults/main.yml`).
- The component still renders this role's `defaults/main.yml` and its two templates
  (`host-upsmon.conf.j2`, `ups-secondary-health.sh.j2`) in place, through `role: nut_host`.
  H7.20a moves them into the component before it deletes the directory.

## Rollback
A revert of H7.33 restores the tasks, and the operator then re-applies the role on daniel-box
after `homelab_host_applied_info` shows the reverted version: `cd /home/ubuntu/server && uv run
ansible-playbook ansible/initial_setup.yml --tags nut_host` (operator-only).
