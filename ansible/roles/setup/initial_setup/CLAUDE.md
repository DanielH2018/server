# initial_setup — host baseline + security hardening

The big host-bring-up role: base packages, per-user Python tooling, SSH/firewall/kernel
hardening, auditing, and file-integrity monitoring. **Not a container role** — a host-setup
role under `ansible/roles/setup/`, run by `initial_setup.yml`, not `deploy.yml`. See repo-root
`CLAUDE.md` for conventions. This is the largest and most fragile setup role — **`--check`
first** and scope with `--tags` when iterating.

## Where it runs
- In `ansible/initial_setup.yml`, after [[config_files]] and before [[sops_setup]] /
  [[docker_install]] — **every host** (Pi-specific tasks self-guard, see below).
- `uv run ansible-playbook ansible/initial_setup.yml --tags "initial_setup"`.

## Granular tags (run one block without the whole role)
Every task carries a block tag (placed right under `name:`), so e.g.
`--tags fail2ban` or `--tags "ssh,firewall"` runs just that slice:
`pi-swap` (Pi swapfile + watchdog-stop preamble) · `apt-upgrade` (the full dist-upgrade)
· `packages` · `tooling` (uv + CLI tools) · `unattended-upgrades` · `fail2ban` · `ssh`
· `crons` (restart / prune / log-truncate / autoremove / dpkg-purge; the prune cron also
answers to `prune`) · `journald` · `tuning` (server CPU governor + swappiness) · `debloat`
(server LXD-snap removal + both-hosts networkd-dispatcher mask) · `git-hooks` · `sysctl` · `firewall` (UFW) · `audit` ·
`file-perms` · `kernel-modules` (blacklist + wireguard) · `accounting` (sysstat + acct) ·
`banners` · `rkhunter` · `login-defs` · `coredumps` · `postfix` · `aide`.
**Fact-dependency rule:** a task whose `register:` feeds other blocks carries ALL its
consumers' tags (e.g. the home-dir resolver is `[tooling, git-hooks]`) — keep that
invariant when adding tasks, or tag-scoped runs die on undefined variables.

## What it does (`tasks/main.yml`, grouped)
- **Pi bring-up (guarded `inventory_hostname == 'daniel-pi'`):** stop the hardware watchdog
  during provisioning, then create/secure/format/persist/activate a swap file — disk swap so
  heavy apt on the 512 MB Zero 2 W doesn't OOM. Also installs Pi-only packages.
- **Packages & tooling:** apt upgrade; base packages; install **uv per-user** (PEP 668-safe on
  24.04+) and the Python CLI tooling as uv tools.
- **SSH:** `.ssh` perms, an `ssh-users` group, sshd hardening, and a `Match` block enabling
  agent/X11 forwarding for `sys_user`. → `notify: Restart SSH`.
- **Firewall (UFW):** default-deny incoming / allow outgoing, **rate-limited** SSH (replaces a
  plain allow), then enable. No WireGuard allow: Docker-published ports (incl. wg-easy's UDP
  port) bypass UFW INPUT via Docker's own chains; a stale Pi-only `51820/udp` allow from the
  pre-port-split era is actively deleted (the Pi listens on 51822).
- **Kernel/network hardening:** IPv4 forwarding, sysctl security knobs, blacklist rare network
  modules, load + persist the WireGuard module.
- **Auditing & accounting:** `auditd` + rules (`notify: Reload audit rules`), `sysstat`,
  process accounting.
- **Integrity & malware:** **AIDE** (install, init DB, weekly check; the package's own
  `dailyaidecheck.timer` is masked — it duplicated the weekly cron nightly with broken
  mail alerting, ~1h20m CPU/night on the Pi) and **rkhunter**
  (install, baseline, post-apt refresh, weekly scan). Both weekly scans run
  `nice -n19 ionice -c3` and are staggered (AIDE Mon 03:00, rkhunter Wed 02:00) — they
  used to overlap Monday mornings at full priority, >1h each on the Pi's 4 slow cores.
- **Login/password policy:** console + network login banners, umask `027`, password hash
  rounds, password-age policy, core dumps disabled (login.defs + systemd).
- **Postfix:** hide the OS banner, disable `VRFY` (`notify: Reload Postfix`).
- **Cron/maintenance:** weekly reboot, Docker image cleanup, ansible.log rotation, weekly
  autoremove + config-remnant purge, and install of the repo Git hooks.
- **Unattended upgrades:** enable periodic security upgrades + local policy.

## Notable
- **Handlers live in the playbook, not this role** (there is no `handlers/main.yml`) — `Restart
  SSH`, `Restart fail2ban`, `Reload audit rules`, `Reload Postfix`, `Restart systemd-journald`
  are defined in `initial_setup.yml`. Same pattern as [[optimize_pi]]: a new `notify:` here needs a matching
  handler added to that playbook.
- **`become` vs HOME:** the `Resolve the deploy user's home directory` task exists because
  `ansible_facts.env.HOME` is root's under the play's `become: true`, but uv / per-user tooling
  must install for the unprivileged deploy user — recent fixes (Pi bring-up era) replaced naive
  `env.HOME` refs with this resolver. Keep new per-user tasks using it, not `env.HOME`.
- **AIDE DB init is slow (~8 min)** and runs with progress monitoring — expect a long pause on
  first run / fresh host; not a hang.
- **Templates:** `templates/98_aide_local.conf.j2` (AIDE exclusions for the Docker homelab) and
  `templates/fail2ban_homelab.conf.j2` (the fail2ban jail).
- Pi-only tasks are individually `when:`-guarded rather than block-scoped, so a server run
  simply skips them.
