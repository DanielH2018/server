# config_files — operator dotfiles (.bashrc / .gitconfig)

Drops the deploy user's shell + git dotfiles onto a host. **Not a container role** — a
host-setup role under `ansible/roles/setup/`, run by `initial_setup.yml`, not `deploy.yml`.
See repo-root `CLAUDE.md` for conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates or playbook entry. -->
- **Applied by:** `initial_setup.yml --tags "config_files"`
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
<!-- /generated_from -->

## Where it runs
- **First** role in `ansible/initial_setup.yml` (before [[initial_setup]], [[sops_setup]],
  [[docker_install]]) — every host, no host guard.
- `uv run ansible-playbook ansible/initial_setup.yml --tags "config_files"`
  (sub-tags `git`, `bash` select one file).

## What it does (`tasks/main.yml`)
- `copy`s the tracked static `files/.gitconfig` and `files/.bashrc` to the deploy user's
  home, **hardcoded to `/home/{{ sys_user }}`** (`owner`/`group` `sys_user`, `mode 0644`,
  `backup: true` → an existing file is preserved as a timestamped `.bak` when content
  changes). The play runs become:true, so a former become:false `echo $HOME` task resolved
  the home (to avoid shipping these dotfiles to /root); that was dropped 2026-06-10
  (commit 6f49a152) — the home is just `/home/{{ sys_user }}`, hardcoded directly to match
  [[sops_setup]]'s `.bashrc` path.

## Notable
- **The SOPS export lives in `files/.bashrc`.** `export SOPS_AGE_KEY_FILE=…` is part of the
  tracked file, so a `--tags config_files` run keeps it. Until #2319, [[sops_setup]] appended
  it with `lineinfile` instead, and a `config_files`-only run stripped it until the next
  `sops_setup`. The chezmoi-managed hosts carry the same line in the dotfiles repo's
  `home/dot_bashrc`, above `ble-attach`.
- **This role overwrites whatever `.bashrc` a host has, chezmoi's included.** It has no host
  guard, and `files/.bashrc` is not chezmoi's version: it lacks the `ble-attach` tail. A
  `--tags config_files` run against daniel-box or daniel-server replaces the chezmoi file
  (keeping a `.bak`).
- Dotfiles are static — edit `files/.bashrc` / `files/.gitconfig` directly (no Jinja).
