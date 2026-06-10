# config_files — operator dotfiles (.bashrc / .gitconfig)

Drops the deploy user's shell + git dotfiles onto a host. **Not a container role** — a
host-setup role under `ansible/roles/setup/`, run by `initial_setup.yml`, not `deploy.yml`.
See repo-root `CLAUDE.md` for conventions.

## Where it runs
- **First** role in `ansible/initial_setup.yml` (before [[initial_setup]], [[sops_setup]],
  [[docker_install]]) — every host, no host guard.
- `ansible-playbook ansible/initial_setup.yml --tags "config_files"`
  (sub-tags `git`, `bash` select one file).

## What it does (`tasks/main.yml`)
- Resolves the **deploy user's** home (become:false `echo $HOME` — the play runs
  become:true, so `ansible_facts.env.HOME` is /root; using it shipped these dotfiles to
  root's home until 2026-06-10), then `copy`s the tracked static `files/.gitconfig` and
  `files/.bashrc` there (`owner`/`group` `sys_user`, `mode 0644`, `backup: true` → an
  existing file is preserved as a timestamped `.bak` when content changes).

## Notable
- **Run-order coupling with SOPS:** [[sops_setup]] later *appends*
  `export SOPS_AGE_KEY_FILE=…` to `.bashrc` via `lineinfile`. The tracked `files/.bashrc`
  does **not** contain that line, and this role uses `copy` (full overwrite). In a normal
  full `initial_setup.yml` run that's fine — config_files runs first, sops_setup re-adds the
  export after. But running **`--tags config_files` alone strips the SOPS export** until the
  next `sops_setup` run. Re-add it by re-running `--tags sops_setup` (or a full setup).
- Dotfiles are static — edit `files/.bashrc` / `files/.gitconfig` directly (no Jinja).
