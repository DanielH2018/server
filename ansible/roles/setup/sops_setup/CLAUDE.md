# sops_setup — emptied; the host packages own sops

The role's tasks are one unconditional `meta: end_role`, under a `# DECIDED` marker in
`ansible/roles/setup/sops_setup/tasks/main.yml`. H7.24b emptied it. It stays until H7.20a,
because `ansible/initial_setup.yml` and `ansible/bootstrap.yml` name it and an edit to either
playbook parks the deployer. Repo-root `CLAUDE.md` has the conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `bootstrap.yml --tags "sops_setup"`; `initial_setup.yml --tags "sops_setup"`;
  ends on hosts where `True` (leading `meta: end_role`)
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
<!-- /generated_from -->

## Where the work went

- `hosts/packages/homelab-tools/package.yml` pins the `sops` binary, at
  `/usr/lib/homelab/bin/sops`. Renovate moves its version and digest together.
- `hosts/components/sops/` ships `/usr/bin/sops`, which execs that binary, and makes the
  package depend on `age`. All three host packages list the component, and each lists the
  role's `/usr/local/bin/sops` as a legacy entry, because `/usr/local/bin` comes first on
  `PATH`.
- `hosts/bootstrap-host.sh` creates the host's own age key, `/etc/homelab/age.key`, which
  `homelab-secrets` decrypts the host's SOPS file with.
- The collections install moved to `initial_setup` under the same `collections` tag, so the
  deployer applies a `requirements.yml` bump as before.
- The role no longer creates the operator's user-home key or seeds `ansible/.sops.yaml`.
  `docs/break-glass.md` records which hosts keep a user-home key, and `ansible/README.md` §5
  has the steps for a new host that runs Ansible.

## Notable

- **DR:** the user-home keys are in **no automated backup**, because nothing outside a
  Longhorn volume is. They are backed up out-of-band (2026-06-06). Since **2026-06-11**
  `ansible/.sops.yaml` also lists an **off-box recovery recipient** (private half only in the
  operator's password manager), so the secrets survive losing every host key.
- **Three creation rules, four kinds of file.** `ansible/.sops.yaml` holds two cluster rules
  ahead of the generic rule, because SOPS takes the first rule that matches and the generic
  rule also matches every cluster manifest. The cluster file (the `cluster-vars` dotenv under
  `vars/`) and the cluster manifests (under `vars/cluster-manifests/`) go to the cluster key
  plus the host keys. `secrets.yml`, and any other YAML under `vars/` or `secrets/`, go to the
  host keys only, so the cluster never decrypts `secrets.yml` (ADR-0022). The file's header
  holds the full runbook.
- **Every `sops` command runs from `ansible/`, or with `--config ansible/.sops.yaml`.** SOPS
  resolves `.sops.yaml` from the working directory, so from the repo root it finds no
  creation rule. `--config` is a global flag and goes before the subcommand (`sops --config
  ansible/.sops.yaml updatekeys <file>`).
- **Onboarding an Nth recipient:** append its public key to the `age:` string of all three
  rules, then from `ansible/` run `sops updatekeys` on `secrets.yml`, the cluster file and each
  cluster manifest that exists, and commit. Removing one deletes its key from all three rules
  and runs `sops rotate -i --rm-age <key>` on the same files, not `updatekeys`; the
  `.sops.yaml` header says why.

## Rollback

A revert of H7.24b restores the tasks and the defaults, and drops the `sops` component and the
legacy entries from the host packages. The operator then re-applies the role on each host after
`homelab_host_applied_info` shows the reverted version, ON daniel-box and daniel-server: `cd
/home/ubuntu/server && uv run ansible-playbook ansible/initial_setup.yml --tags sops-install`,
and from a node for the Pi, with `-e target=daniel-pi` (operator-only). Until then, a host that
took the H7.24b package and then the revert has no `sops` on `PATH`, because reconcile removed
`/usr/local/bin/sops` and dpkg removed `/usr/bin/sops`.
