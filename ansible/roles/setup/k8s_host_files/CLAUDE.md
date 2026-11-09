# `setup/k8s_host_files` — emptied; daniel-box's package retires what it held

The role's tasks are one unconditional `meta: end_role`, under a `# DECIDED` marker in
`ansible/roles/setup/k8s_host_files/tasks/main.yml`. H7.48 emptied it. It stays until H7.20a,
because `ansible/initial_setup.yml` names it and an edit to that playbook parks the deployer.
Repo-root `CLAUDE.md` has the conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "k8s_host_files"` when `inventory_hostname ==
  'daniel-box'`; ends on hosts where `True` (leading `meta: end_role`)
- **Crons / timers:** none (the role ends on every host before its first task)
<!-- /generated_from -->

## Where the work went
D5 moved the host writes of each k8s role here before the role flipped to Flux
(`docs/target-architecture/deploy/d5-bulk-move.md`, *Host-side rows*). Each has left since:

- The directories and files that still have to exist are in host package components: docs'
  site directory in `hosts/components/docs-refresh/`, artifacts' trees in
  `hosts/components/claude-code/`, media-volume's directories in daniel-box's `tmpfiles.d` file,
  the ghcr-mirror GC in `hosts/components/registry-gc/`, crowdsec's edge probe in
  `hosts/components/crowdsec-probe/` and pi-peer-backup's wrapper and key in
  `hosts/components/pi-peer-backup-target/`.
- The absent arms are `legacy:` entries in `hosts/daniel-box/package.yml`, under the H7.48
  comment, and `homelab-reconcile` removes each from a host that still holds it. They cover
  the retired host crons of configarr, janitorr, crowdsec, qbittorrent and traefik with their
  scripts, configarr's and janitorr's drift fragments, the push store's GC Job manifest and the
  apply-time SHA-256 record directory. Reconcile removes a directory only when it is empty, so
  the readers' code directories and observability's dashboard staging trees have no entry.
  Nothing reads them once their crons are gone. `ansible/tests/repo/_retired_cron_row.py` holds the crons and fragments to that list.

## Rollback
A revert of H7.48 restores the task files and their tests. The tick then applies the role on
daniel-box on its next run, and the absent arms find nothing left to remove.
