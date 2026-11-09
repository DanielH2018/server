# Security Tools Reference

This document covers the security and monitoring tools deployed by `ansible/roles/setup/initial_setup/tasks/main.yml` — what each one does and how to read its output.

---

## rkhunter (Rootkit Hunter)

**What it does:** Scans the system for rootkits, backdoors, and suspicious files. Checks whether system binaries (`ls`, `ps`, `netstat`, etc.) have been replaced by trojaned versions, looks for hidden processes or ports, and checks for files/directories that rootkits commonly create.

**Schedule:** Weekly, Wednesday at 2:00 AM, on every host (staggered off the Monday AIDE check). The cron runs `/usr/local/bin/rkhunter-check.sh`, which writes the warnings to syslog tagged `rkhunter` and writes a verdict textfile that `RkhunterScanFailed` and `RkhunterScanNotReporting` read. A Kuma tile took the verdict until M6 batch B9 retired it.

**What the verdict means.** rkhunter exits 1 both when it finds a warning and when it fails, so the script reads `/var/log/rkhunter.log` instead (#4598). A scan completed only if this run rewrote the log and the log holds its `System checks summary`.

| Scan | Tile | Message |
|------|------|---------|
| completed, no warning, suspect file or possible rootkit | UP | the counts, all 0 |
| completed, `Possible rootkits` above 0 | DOWN | `possible rootkit found` with the counts |
| completed, `Suspect files` above 0 | DOWN | `suspect file found` with the counts |
| completed with a warning | DOWN | `rkhunter warned` with the counts and the first warning |
| did not complete | DOWN | rkhunter's exit code and its first error line |

No verdict for a week and 2 hours also pages, through `RkhunterScanNotReporting`, which means the cron stopped running or the scan never finished.

Every warning pages (#4638). Before that change, every host warned every week from two benign causes. rkhunter reported `/etc/.updated` and `/etc/.resolv.conf.systemd-resolved.bak` as hidden files on all three hosts. It also reported `PermitRootLogin` unset on daniel-pi, because rkhunter reads only the main `/etc/ssh/sshd_config` and the Pi hardens a drop-in. `initial_setup` renders `/etc/rkhunter.conf.local` to allowlist both causes, so a healthy week has no warnings. The `DECIDED:` marker in `ansible/roles/setup/initial_setup/templates/rkhunter-check.sh.j2` holds the evidence. The script's verdict goes to syslog tagged `rkhunter-check` as `status=<up|down> <message>`.

To clear a warning, find its cause first. rkhunter reports a user or group added to `/etc/passwd` or `/etc/group` once, the week after a package or the operator adds it. Once you have confirmed the account is yours, run `sudo /usr/local/bin/rkhunter-check.sh` again; rkhunter stored the new account list on the run that warned, so the rerun reads UP. A benign warning that recurs every week needs an `ALLOW*` line in `ansible/roles/setup/initial_setup/templates/rkhunter.conf.local.j2`, applied with `--tags rkhunter`.

To prove the alert pages, make rkhunter fail to start on one host and run the script by hand. `ionice` then cannot execute it and exits 126 without touching the log, so the verdict goes DOWN within seconds:

```bash
sudo chmod -x /usr/bin/rkhunter
sudo /usr/local/bin/rkhunter-check.sh
sudo chmod +x /usr/bin/rkhunter
```

The verdict stays DOWN until the next run. To clear it, run `sudo /usr/local/bin/rkhunter-check.sh` again; a scan takes two to three minutes.

**Check for findings:**

```bash
# From the weekly cron
sudo journalctl -t rkhunter -t rkhunter-check --since -7d
sudo grep Warning /var/log/rkhunter.log

# Full interactive scan with detailed output
sudo rkhunter --check --nocolors --skip-keypress
```

The full log is always at `/var/log/rkhunter.log`. Warnings appear as `[ Warning ]` lines. At the end of a run you get a summary:

```
System checks summary
=====================
Files checked: 147       Suspect files: 0
Rootkits checked: 497    Possible rootkits: 0
```

**False positives:** rkhunter is noisy after package upgrades — it warns about changed binary hashes. **Routine upgrades are already handled:** the `integrity-scans` host component ships a `DPkg::Post-Invoke` hook, `/etc/apt/apt.conf.d/91homelab-rkhunter-propupd`, that starts `homelab-rkhunter-propupd.service` after every `dpkg` run, unattended-upgrades included. The unit waits for apt to release the `dpkg` lock, then runs `rkhunter --propupd`, and runs it again if `dpkg` changed packages meanwhile. rkhunter's own `APT_AUTOGEN` hook ran the same refresh inside apt and held the `dpkg` lock for about 2 minutes on the Pi, so the component sets `APT_AUTOGEN="false"` (#7680). Check a refresh with `journalctl -t rkhunter-propupd`. That's the point — the only file-property changes the weekly scan flags are ones *not* explained by a trusted package event.

So don't reflexively run this after an upgrade. Reset the baseline by hand only after an intentional change apt didn't make (a manually installed binary, a hand-edited config in a watched path), and only once you've confirmed the warning is yours:

```bash
sudo rkhunter --propupd
```

Running it to silence a warning you haven't explained re-blesses whatever caused it, which is exactly the tampering the weekly `--check` exists to catch.

---

## AIDE (Advanced Intrusion Detection Environment)

**What it does:** Takes a cryptographic snapshot of the filesystem (hashes, permissions, ownership, timestamps) and detects any additions, modifications, or deletions since that snapshot was taken.

**Schedule:** Weekly, Monday at 3:00 AM, on every host. `homelab-aide-check.timer` runs `/usr/lib/homelab/aide-check`, which writes the report to syslog tagged `aide` and writes a verdict textfile that `AideCheckFailed` and `AideCheckNotReporting` read. A Kuma tile took the verdict until M6 batch B9 retired it.

Every `aide` command needs `--config=/etc/aide/aide.conf`. AIDE 0.18 has no default config path, so without it the command exits with `ERROR: missing configuration` and checks nothing.

**What the verdict means.** The script runs `aide --update`, reads its exit code (#4594), and classifies each difference against the package database (#4599):

| Exit | Differences | Tile | Baseline |
|------|-------------|------|----------|
| 0 | none | UP | advanced |
| 1-7 | every one explained, or outside the paged trees | UP, with the counts | advanced |
| 1-7 | at least one no package explains | DOWN, naming the first three | kept |
| anything else | not read | DOWN, with AIDE's first error line | kept |

No verdict for a week and 6 hours also pages, through `AideCheckNotReporting`, which means the timer stopped running or the scan never finished.

A difference pages when it falls in one of these and no package explains it:

- The system trees: `/usr` except `/usr/local`, `/bin`, `/sbin`, `/lib*` and `/boot`. A file pages unless a package owns it and its MD5 matches the package's. A package-owned symlink pages unless it still resolves to a package file. A distro link whose target is missing, such as the kernel headers' `rust` link into a package that is not installed, is explained when the target is in a system tree outside `/usr/local` and its owner has a `dpkg.log` entry since the previous baseline. A removed path pages while a package still owns it.
- A package-owned file under `/etc` whose content differs from the package's copy. Ansible writes `/etc` files no package owns on every merge, so those are logged and do not page. A `homelab-*` package's `/etc` link into `/run/homelab-secrets/`, where the secrets unit writes each secret, is explained.
- The login, privilege, startup and code-loading files, packaged or not: sudoers, `/etc/ssh`, PAM and `/etc/security`, cron, `passwd`, `shadow`, `group`, `gshadow`, the loader config (`ld.so.preload`, `ld.so.conf.d`), `/etc/environment` and the shell startup files (`/etc/profile.d`, `bash.bashrc`, root's), apt hooks in `apt.conf.d`, device rules in `/etc/udev/rules.d`, module config, systemd units and drop-ins in `/etc/systemd/system`, apt sources and keys (`sources.list.d`, `trusted.gpg.d`, `keyrings`), `/etc/update-motd.d`, `/etc/init.d`, `/etc/rc.local`, everything in `/usr/local/sbin`, and every `authorized_keys`. The units the setup roles install, such as the deployer's and the kuma-check timers, are listed in the script's `ROLE_WRITTEN` and page only when a package owns them. `ansible/tests/setup/test_aide_check_role_written.py` fails when a role installs a unit the list omits.
- An added or changed `/usr/local/bin` entry named like a command a package installs, because root's `PATH` reaches `/usr/local/bin` first. The roles' own scripts there have no such twin and are logged.
- An entry in any of those, or a package-owned `/etc` file, whose owner, group or permissions changed. A package digest cannot see a mode change. An `install` or `upgrade` entry in `dpkg.log` for a package that owns the path explains the change, but never for a login, privilege or startup file, and for a `homelab-*` package only under `/etc`, because those packages upgrade several times a day. The content is still checked against the package's digest.

Files that package triggers regenerate are explained by the `dpkg.log` entries since the previous baseline, not by a package run of any kind. The initial RAM disk, the kernel links and the module index need a kernel, `initramfs-tools` or `kmod` entry. GRUB's files need a GRUB or kernel entry. The EFI loaders under `/boot/efi/EFI/` need a `grub-efi` or `shim` entry. Their inode number is not checked, because vfat assigns new ones at every mount. A compiled `.pyc` needs an entry for the package that owns its source, or for Python itself. Other trigger caches need any package entry. The window opens 6 hours before the previous baseline was written, so a package run during that scan counts. `/boot/grub/grubenv` is the one exact path exempt from the requirement, because GRUB rewrites it at every boot. The top-level links `/bin`, `/sbin` and `/lib*` page unless they point at their `/usr` counterpart. A path at either end of a package diversion (`dpkg-divert`) must match the digest one of the two packages records. Homes, `/var`, `/opt` and the rest of `/usr/local` are logged and never page: the first two change with ordinary use, and Ansible rewrites the last on every merge. The script's header has the full lists, and its `DECIDED:` marker has the measurement behind them.

Advancing the baseline after every UP run makes each report cover one week. A DOWN keeps the old baseline, so the change stays in every report until someone reviews it.

**Reviewing a DOWN:** the journal lists every unexplained path with its reason, under `aide-check`:

```bash
sudo journalctl -t aide-check --since -7d | grep unexplained
```

Once every path is accounted for, accept the run's database as the baseline. This takes the scan lock and moves `aide.db.new` over `aide.db`. It scans nothing and writes no verdict, and the alert clears on the next weekly run:

```bash
sudo /usr/lib/homelab/aide-check --accept
```

It refuses when `aide.db` is missing, because a deleted baseline must stay an error rather than become a fresh, trusted one. `hosts/bootstrap-host.sh` creates the first baseline.

The script logs at most 2000 report lines. A longer report ends with a `report truncated` line. To see the rest, run the check by hand. Its verdict goes to syslog tagged `aide-check` as `status=<up|down> <message>`.

To prove the alert pages, force an error exit on one host and run the script by hand. Rename the database, run `sudo /usr/lib/homelab/aide-check`, then put the database back. AIDE exits 18 at once, and the verdict goes DOWN with `open (read-only) failed for file '/var/lib/aide/aide.db'` in its message:

```bash
sudo mv /var/lib/aide/aide.db /var/lib/aide/aide.db.held
sudo /usr/lib/homelab/aide-check
sudo mv /var/lib/aide/aide.db.held /var/lib/aide/aide.db
```

The verdict stays DOWN until the next run. To clear it, run `sudo /usr/lib/homelab/aide-check` again. That run scans the whole filesystem, so on the Pi it takes over an hour.

**Check for findings:**

```bash
# From the weekly timer
sudo journalctl -t aide -t aide-check --since -7d

# Run a manual check, which leaves the baseline alone
sudo aide --config=/etc/aide/aide.conf --check
```

Output shows exactly what changed:

```text
AIDE found differences between database and filesystem!!

Summary:
  Added entries:   2
  Removed entries: 0
  Changed entries: 5

Changed entries:
f =.... mc..H.. . : /etc/ssh/sshd_config
f =...p mc..H.. . : /etc/passwd
```

The 18-character column on the left says what changed, in the order `YlZbpugamcinHAXSEC`: `p` = permissions, `u` = user/owner, `g` = group, `H` = content hash, and so on. An added entry shows `+` in every column and a removed one `-`.

The initial database lives at `/var/lib/aide/aide.db`. The first run (`aideinit`) takes 10–20 minutes on this server due to file count.

---

## auditd (Linux Audit Daemon)

**What it does:** Kernel-level event logging — captures file access, privilege escalations, and config changes directly from the kernel, bypassing userspace logging. Configured to watch identity files, SSH config, and sudoers.

**Watched paths and their keys:**

--8<-- "assets/generated/fragments/auditd-watches.md"

**Check for findings:**

```bash
# All audit events today
sudo ausearch -ts today

# Who touched sudoers
sudo ausearch -k actions

# Who touched SSH config
sudo ausearch -k sshd

# Who touched passwd/shadow/group
sudo ausearch -k identity

# Raw log
sudo tail -f /var/log/audit/audit.log
```

Each result shows the user, process, timestamp, and whether the action succeeded.

---

## sysstat

**What it does:** Collects system performance data on a schedule (CPU, memory, I/O, network) and stores historical records. Not a security tool directly, but useful for spotting anomalies — for example, unexpected I/O at 3 AM may indicate something scanning the disk.

**Check historical data:**

```bash
# CPU usage history for today
sar -u

# Memory history
sar -r

# Disk I/O history
sar -d

# Network history
sar -n DEV

# Narrow to a time range
sar -u 08:00:00 10:00:00

# Query a past date (replace DD with day number)
sar -u -f /var/log/sysstat/saDD
```

Historical data is stored in `/var/log/sysstat/`.

---

## acct (Process Accounting)

**What it does:** Records every command executed on the system — who ran it, when, how long it ran, and how much CPU it used. Runs at the kernel level, so unlike bash history it cannot be cleared or disabled by a normal user.

**Check for findings:**

```bash
# All recent commands, newest first
sudo lastcomm

# Filter by user
sudo lastcomm ubuntu

# Filter by command name
sudo lastcomm sudo

# Summary of resource usage by user
sudo sa -u
```

Output example:
```
bash    S   ubuntu   pts/0   0.00 secs Mon May 30 23:14
sudo    S   ubuntu   pts/0   0.01 secs Mon May 30 23:14
apt-get S   root     pts/0   2.34 secs Mon May 30 23:14
```

The `S` flag means the process ran with superuser privileges.

---

## fail2ban (Intrusion Prevention)

**What it does:** Watches auth logs and bans IPs that show repeated failed logins, enforcing the ban at the firewall (`banaction = ufw`). Jails are enabled for **sshd** and **postfix**, plus a **recidive** jail that re-bans repeat offenders for much longer. Configured in `/etc/fail2ban/jail.d/homelab.conf` (deployed from `ansible/roles/setup/initial_setup/templates/fail2ban_homelab.conf.j2`).

**Schedule:** Runs continuously as a systemd service — it reacts to log events in real time, not on a cron.

**Ban policy:**

<!-- Generated from the fail2ban jail template; edit that. -->
--8<-- "assets/generated/fragments/fail2ban-jails.md"

For `recidive` a "failure" is a ban by another jail, so its row reads as bans within the
window.

**Check for findings:**

```bash
# Overall status + which jails are active
sudo fail2ban-client status

# Currently-banned IPs for a jail
sudo fail2ban-client status sshd

# Ban / unban history
sudo grep -E 'Ban|Unban' /var/log/fail2ban.log

# Manually unban a false-positive IP
sudo fail2ban-client set sshd unbanip <IP>
```

Because bans are enforced through UFW, a banned IP also shows up in `sudo ufw status numbered`.

---

## unattended-upgrades (Automatic Security Patching)

**What it does:** Automatically installs security updates so the host doesn't drift behind on known-vulnerable packages. The distro's `50unattended-upgrades` enables the **security** origins; a local drop-in (`/etc/apt/apt.conf.d/52unattended-upgrades-local`) sets the homelab policy: **no automatic reboot** (the weekly "system restart" cron owns reboots) plus cleanup of obsolete kernels and unused dependencies. `/etc/apt/apt.conf.d/20auto-upgrades` turns on the daily package-list update, download, and unattended run, with an autoclean every 7 days.

**Schedule:** Daily, via systemd's `apt-daily` / `apt-daily-upgrade` timers.

**Check for findings:**

```bash
# What was installed, and when
sudo grep -E 'Package|Install|Upgrade' /var/log/unattended-upgrades/unattended-upgrades.log

# Timer schedule and last run
systemctl list-timers 'apt-daily*'

# Dry-run what would be upgraded right now
sudo unattended-upgrade --dry-run --debug
```

> A lingering `/var/run/reboot-required` (for example, after a kernel update) is **intentional** — reboots are deferred to the weekly restart cron, not applied automatically.

---

## Quick Reference

| Concern | Command |
|---------|---------|
| Sensitive file modified | `sudo ausearch -k identity` or `-k sshd` |
| Rootkit / backdoor suspicion | `sudo rkhunter --check` |
| Unexpected file change | `sudo aide --config=/etc/aide/aide.conf --check` |
| `RkhunterScanFailed` | `sudo journalctl -t rkhunter-check -t rkhunter --since -7d` and `/var/log/rkhunter.log` |
| `AideCheckFailed` | `sudo journalctl -t aide-check -t aide --since -7d` |
| Unusual commands run as root | `sudo lastcomm` |
| Unusual I/O or CPU activity | `sar -d` / `sar -u` |
| Brute-force attempts / banned IPs | `sudo fail2ban-client status sshd` |
| Auto-patch history | `sudo grep Install /var/log/unattended-upgrades/unattended-upgrades.log` |
| Any security cron warning | `sudo grep -E 'rkhunter\|aide\|apt-autoremove\|dpkg-purge' /var/log/syslog` |
