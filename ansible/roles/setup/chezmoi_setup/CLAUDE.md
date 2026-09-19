# `setup/chezmoi_setup` — the connecting user's dotfiles, via chezmoi

Installs the chezmoi binary into `~/.local/bin`, seeds `~/.config/chezmoi/chezmoi.toml` from
`templates/chezmoi-seed.toml.j2` so `chezmoi init` never waits on a TTY, then clones
`chezmoi_setup_repo` (`DanielH2018/dotfiles`, private) and applies it. Applied by
`initial_setup.yml` (`--tags chezmoi`) where `has_chezmoi` is set; it runs after
`github_cli`, because the private clone needs the `gh` credential helper that role wires up.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates or playbook entry. -->
- **Applied by:** `initial_setup.yml --tags "chezmoi"` when `has_chezmoi`
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
<!-- /generated_from -->

- **Every task is `become: false`.** chezmoi manages a USER's home, and the play runs escalated
  for the OS hardening; inheriting `become` deploys the dotfiles into `/root`.
- **The home directory is `/home/{{ sys_user }}`, not `ansible_env.HOME`.** Facts are
  gathered under the play's `become: true`, so `ansible_env.HOME` reads `/root` even inside a
  `become: false` task — it failed on daniel-box with `Permission denied: /root/.local`.
- **The seed answers the two `promptOnce` questions** in the dotfiles' `.chezmoi.toml.tmpl`
  (`chezmoi_setup_profile`, `chezmoi_setup_work`). chezmoi records them on
  first init and reuses them; changing a seed value after that needs the config edited on the
  host, not just a re-run.
- **An unauthenticated `gh` fails with a usable message**, not with git's
  `could not read Username`: the role checks `gh auth status` before cloning.
- **The distro packages the dotfiles scripts cannot install themselves** are one apt task
  here (`chezmoi_setup_distro_packages`); a new dotfiles dependency that needs root goes
  there, never into a hand install on the host.
