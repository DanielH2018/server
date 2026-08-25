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
· `packages` · `tooling` (uv + CLI tools) · `unattended-upgrades` · `sudo-timestamp` · `fail2ban` · `ssh`
· `crons` (restart / prune / log-truncate / autoremove / dpkg-purge / infra-map; the prune cron
also answers to `prune`, the daniel-box-only infrastructure-map refresh to `infra-map`, and the
fwupd-gated weekly firmware update to `firmware`)
· `journald` · `tuning` (server CPU governor + swappiness) · `debloat`
(server LXD-snap removal + both-hosts networkd-dispatcher mask) · `git-hooks` · `sysctl` · `firewall` (UFW) · `audit` ·
`file-perms` · `kernel-modules` (blacklist + wireguard) · `igpu` (i915 GuC for QuickSync,
`has_igpu` hosts only) · `accounting` (sysstat + acct) ·
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
- **Unattended upgrades:** `20auto-upgrades` turns the periodic timers on;
  `52unattended-upgrades-local` sets no-automatic-reboot (the Sunday 07:30 restart cron owns
  reboots) plus obsolete-kernel cleanup, and **appends** `unattended_upgrades_origins_patterns`
  (`group_vars/all.yml`) as `Origins-Pattern::` entries. Since 2026-08-24 that list adds the
  `-updates` pocket and the GitHub CLI repo, so the "N updates can be applied immediately" MOTD
  line no longer accrues a permanent backlog. Set the var to `[]` on a host for security-only.
  Two traps, both of which look correct at every stage except the one that matters:
  - **Use `Origins-Pattern`, not `Allowed-Origins`.** The latter is the legacy `origin:archive`
    form and is rewritten to `o=X,a=Y`, so it only matches a repo publishing a `Suite:` field.
    The gh repo has none (`origin='gh', archive='', codename='stable'`), so `gh:stable` matched
    nothing while `apt-config dump` listed it. ENFORCED by
    `ansible/tests/test_unattended_origins_pattern.py`.
  - **Use the `::` append syntax.** A `{ ... }` block reads as a replacement, and a replacement
    drops the `-security` and ESM pockets.

  Verify with `apt-config dump Unattended-Upgrade::Allowed-Origins` (must still list
  `-security`) **and** `apt-config dump Unattended-Upgrade::Origins-Pattern` (must list the
  extras). Note `unattended-upgrade --dry-run` needs root, so the only unprivileged proof that
  a pattern matches is reading `archive`/`codename` off the package file via python-apt.
- **SSH:** `.ssh` perms, an `ssh-users` group, sshd hardening, and a `Match` block re-enabling
  forwarding for `sys_user` (the global config disables agent/X11/TCP forwarding). Since #397
  that block sets `AllowTcpForwarding all`, not `local` — **both** directions, plus
  `AllowStreamLocalForwarding remote` and `StreamLocalBindUnlink yes`, for the clipboard
  bridge's reverse unix-socket forward. `local` alone does not work and the reason is not
  guessable: sshd builds the channel layer from `AllowTcpForwarding` alone, so excluding
  `FORWARD_REMOTE` refuses every remote forward — unix-domain included — before
  `AllowStreamLocalForwarding` is ever read. The comment at `tasks/access.yml:172-196` carries
  the full derivation and the log line that distinguishes the two refusals.

  It is a real widening. It is acceptable because `sys_user` already has a full shell and can
  run `ssh -R` itself, so this restricts nothing that account could not already do —
  `AllowTcpForwarding` bounds a key used purely as a tunnel, not an interactive account.
  `GatewayPorts` stays at its default `no`, so a reverse forward binds loopback only.
  → `notify: Restart SSH`.
- **Firewall (UFW):** default-deny incoming / allow outgoing, **rate-limited** SSH (replaces a
  plain allow), then enable. No WireGuard allow: Docker-published ports (incl. wg-easy's UDP
  port) bypass UFW INPUT via Docker's own chains; a stale Pi-only `51820/udp` allow from the
  pre-port-split era is actively deleted (the Pi listens on 51822).
- **sudo credential cache:** `/etc/sudoers.d/10-timestamp` sets `timestamp_type=global`
  (+ a 60-min timeout), so one authentication covers every tmux pane and every shell with no
  tty. Without it, sudo keys the ticket on its parent pid when no terminal is present and each
  Claude Code `!` command re-prompts. Written with `validate: visudo -cf %s` — a malformed
  drop-in locks sudo out, and sudo is the only write path to the cluster.
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
- **Postfix:** bind `inet_interfaces = loopback-only` (send-only local mailer — drops the
  `0.0.0.0:25` listener off the network surface; `notify: Restart Postfix`, a reload won't
  rebind sockets), hide the OS banner, disable `VRFY` (`notify: Reload Postfix`).
- **Cron/maintenance:** weekly reboot, Docker image cleanup, ansible.log rotation, weekly
  autoremove + config-remnant purge, install of the repo Git hooks, and (daniel-box only) the
  15-minute infrastructure-map refresh that regenerates the HTML artifact from
  `scripts/infra_map/gen_infra_map.py`.
- **Unattended upgrades:** enable periodic security upgrades + local policy.

## Notable
- **Handlers live in the playbook, not this role** (there is no `handlers/main.yml`) — `Restart
  SSH`, `Restart fail2ban`, `Reload audit rules`, `Reload Postfix`, `Restart Postfix`, `Restart
  systemd-journald`, `Rebuild initramfs for i915`, `Warn that a reboot is required for i915 GuC`
  are defined in `initial_setup.yml`. Same pattern as [[optimize_pi]]: a new `notify:` here needs a matching
  handler added to that playbook.
- **iGPU (`igpu` tag)** writes `/etc/modprobe.d/i915.conf` (`options i915 enable_guc=2`) and
  rebuilds the initramfs, but **never reboots** — unlike `Reboot Pi`, this fires on the machine
  running the whole homelab, so it only prints a reboot reminder.
  **Gated on `has_igpu` AND `/sys/module/i915` existing.** `has_igpu` alone is the wrong gate:
  it means "pass `/dev/dri` through to jellyfin/tdarr", which is vendor-neutral (AMD does VAAPI
  through the same node), while `enable_guc` is Intel-only. The driver check keeps this correct
  on a future AMD/NVIDIA host without hostname-gating it — deliberately, since the repo is
  removing `daniel-server` literals ahead of the master-node migration.
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
