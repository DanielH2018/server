# Security Tools Reference

This document covers the security and monitoring tools deployed by `ansible/roles/setup/initial_setup/tasks/main.yml` — what each one does and how to read its output.

---

## rkhunter (Rootkit Hunter)

**What it does:** Scans the system for rootkits, backdoors, and suspicious files. Checks whether system binaries (`ls`, `ps`, `netstat`, etc.) have been replaced by trojaned versions, looks for hidden processes or ports, and checks for files/directories that rootkits commonly create.

**Schedule:** Weekly, Wednesday at 2:00 AM, on every host (staggered off the Monday AIDE check). The cron runs `/usr/local/bin/rkhunter-check.sh`, which writes the warnings to syslog tagged `rkhunter` and pushes a verdict to that host's **rkhunter Scan (&lt;host&gt;)** Kuma tile.

**What the tile means.** rkhunter exits 1 both when it finds a warning and when it fails, so the script reads `/var/log/rkhunter.log` instead (#4598). A scan completed only if this run rewrote the log and the log holds its `System checks summary`.

| Scan | Tile | Message |
|------|------|---------|
| completed, `Possible rootkits: 0` | UP | the warning, suspect-file and possible-rootkit counts |
| completed, `Possible rootkits` above 0 | DOWN | `possible rootkit found` with the counts |
| did not complete | DOWN | rkhunter's exit code and its first error line |

No push for a week and 2 hours also reads DOWN, which means the cron stopped running or the scan never finished.

Warnings read UP on purpose. Every host warns every week: `/etc/.updated` and `/etc/.resolv.conf.systemd-resolved.bak` are reported as hidden files on all three hosts, and daniel-pi reports `PermitRootLogin` unset. A DOWN on warnings would be red every Wednesday. Read the warning count in the tile's message. Paging on a warning or a suspect file is tracked in #4638. The `DECIDED:` marker in `ansible/roles/setup/initial_setup/templates/rkhunter-check.sh.j2` holds the evidence. The script's verdict goes to syslog tagged `rkhunter-check` as `status=<up|down> <message>`.

To prove the tile pages, make rkhunter fail to start on one host and run the script by hand. `ionice` then cannot execute it and exits 126 without touching the log, so the tile goes DOWN within seconds:

```bash
sudo chmod -x /usr/bin/rkhunter
sudo /usr/local/bin/rkhunter-check.sh
sudo chmod +x /usr/bin/rkhunter
```

The tile stays DOWN until the next run. To clear it, run `sudo /usr/local/bin/rkhunter-check.sh` again; a scan takes two to three minutes.

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

**False positives:** rkhunter is noisy after package upgrades — it warns about changed binary hashes. **Routine upgrades are already handled:** `initial_setup` sets `APT_AUTOGEN=true` in `/etc/default/rkhunter`, so `--propupd` runs from the package-manager hook after every apt install/upgrade, including unattended-upgrades. That's the point — the only file-property changes the weekly scan flags are ones *not* explained by a trusted package event.

So don't reflexively run this after an upgrade. Reset the baseline by hand only after an intentional change apt didn't make (a manually installed binary, a hand-edited config in a watched path), and only once you've confirmed the warning is yours:

```bash
sudo rkhunter --propupd
```

Running it to silence a warning you haven't explained re-blesses whatever caused it, which is exactly the tampering the weekly `--check` exists to catch.

---

## AIDE (Advanced Intrusion Detection Environment)

**What it does:** Takes a cryptographic snapshot of the filesystem (hashes, permissions, ownership, timestamps) and detects any additions, modifications, or deletions since that snapshot was taken.

**Schedule:** Weekly, Monday at 3:00 AM, on every host. The cron runs `/usr/local/bin/aide-check.sh`, which writes the report to syslog tagged `aide` and pushes a verdict to that host's **AIDE Integrity (&lt;host&gt;)** Kuma tile.

Every `aide` command needs `--config=/etc/aide/aide.conf`. AIDE 0.18 has no default config path, so without it the command exits with `ERROR: missing configuration` and checks nothing.

**What the tile means.** The script reads AIDE's exit code (#4594):

| Exit | Tile | Message |
|------|------|---------|
| 0 | UP | no differences from the baseline |
| 1-7 | UP | the added, removed and changed counts from the report's Summary |
| anything else | DOWN | AIDE's first error line: the check did not complete |

No push for a week and 6 hours also reads DOWN, which means the cron stopped running or the scan never finished.

Differences read UP on purpose. Unattended-upgrades installs packages on every host every week, and AIDE reports every file a package touched, so a DOWN on differences would be red every Monday. An UP tile therefore means that AIDE completed a check, not that nothing changed. Read the counts in the tile's message, and read the list in the journal. No alert fires on a changed file, however sensitive. Paging on a change that no package upgrade explains is tracked in #4599. The counts are cumulative: they grow every week until someone updates the baseline (below). The `DECIDED:` marker in `ansible/roles/setup/initial_setup/templates/aide-check.sh.j2` holds the evidence.

The script logs at most 2000 report lines. A longer report ends with a `report truncated` line. To see the rest, run the check by hand. Its verdict goes to syslog tagged `aide-check` as `status=<up|down> <message>`.

To prove the tile pages, force an error exit on one host and run the script by hand. Rename the database, run `sudo /usr/local/bin/aide-check.sh`, then put the database back. AIDE exits 18 at once, and the tile goes DOWN with `open (read-only) failed for file '/var/lib/aide/aide.db'` in its message:

```bash
sudo mv /var/lib/aide/aide.db /var/lib/aide/aide.db.held
sudo /usr/local/bin/aide-check.sh
sudo mv /var/lib/aide/aide.db.held /var/lib/aide/aide.db
```

The tile stays DOWN until the next run. To clear it, run `sudo /usr/local/bin/aide-check.sh` again. That run scans the whole filesystem, so on the Pi it takes over an hour.

**Check for findings:**

```bash
# From the weekly cron
sudo journalctl -t aide -t aide-check --since -7d

# Run a manual check
sudo aide --config=/etc/aide/aide.conf --check
```

Output shows exactly what changed:

```
AIDE found differences between database and filesystem!!

Summary:
  Added entries:   2
  Removed entries: 0
  Changed entries: 5

Changed entries:
  f ... : /etc/ssh/sshd_config
  f ... : /etc/passwd
```

The letters on the left indicate what changed: `p` = permissions, `u` = user/owner, `s` = size, `sha256` = content hash, etc.

**Updating the baseline:** After any intentional system change (package update, config edit, re-running `initial_setup.yml`), update the baseline or AIDE keeps reporting those changes:

```bash
sudo aide --config=/etc/aide/aide.conf --update
sudo mv /var/lib/aide/aide.db.new /var/lib/aide/aide.db
```

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
| rkhunter tile DOWN | `sudo journalctl -t rkhunter-check -t rkhunter --since -7d` and `/var/log/rkhunter.log` |
| AIDE tile DOWN | `sudo journalctl -t aide-check -t aide --since -7d` |
| Unusual commands run as root | `sudo lastcomm` |
| Unusual I/O or CPU activity | `sar -d` / `sar -u` |
| Brute-force attempts / banned IPs | `sudo fail2ban-client status sshd` |
| Auto-patch history | `sudo grep Install /var/log/unattended-upgrades/unattended-upgrades.log` |
| Any security cron warning | `sudo grep -E 'rkhunter\|aide\|apt-autoremove\|dpkg-purge' /var/log/syslog` |
