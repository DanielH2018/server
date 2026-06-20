# Security Tools Reference

This document covers the security and monitoring tools deployed by `ansible/roles/setup/initial_setup/tasks/main.yml` — what each one does and how to read its output.

---

## rkhunter (Rootkit Hunter)

**What it does:** Scans the system for rootkits, backdoors, and suspicious files. Checks whether system binaries (`ls`, `ps`, `netstat`, etc.) have been replaced by trojaned versions, looks for hidden processes or ports, and checks for files/directories that rootkits commonly create.

**Schedule:** Weekly, Monday at 2:00 AM. Warnings-only output is sent to syslog tagged `rkhunter`.

**Check for findings:**

```bash
# From the weekly cron
sudo grep rkhunter /var/log/syslog

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

**False positives:** rkhunter is noisy after package upgrades — it will warn about changed binary hashes. After any intentional system update, reset the baseline:

```bash
sudo rkhunter --propupd
```

---

## AIDE (Advanced Intrusion Detection Environment)

**What it does:** Takes a cryptographic snapshot of the filesystem (hashes, permissions, ownership, timestamps) and detects any additions, modifications, or deletions since that snapshot was taken.

**Schedule:** Weekly, Monday at 3:00 AM. Output is sent to syslog tagged `aide`.

**Check for findings:**

```bash
# From the weekly cron
sudo grep '\baide\b' /var/log/syslog

# Run a manual check
sudo aide --check
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

**Updating the baseline:** After any intentional system change (package update, config edit, re-running `initial_setup.yml`), update the baseline or AIDE will keep reporting those changes:

```bash
sudo aide --update
sudo mv /var/lib/aide/aide.db.new /var/lib/aide/aide.db
```

The initial database lives at `/var/lib/aide/aide.db`. The first run (`aideinit`) takes 10–20 minutes on this server due to file count.

---

## auditd (Linux Audit Daemon)

**What it does:** Kernel-level event logging — captures file access, privilege escalations, and config changes directly from the kernel, bypassing userspace logging. Configured to watch identity files, SSH config, and sudoers.

**Watched paths and their keys:**

| Key | Paths watched |
|-----|---------------|
| `identity` | `/etc/passwd`, `/etc/group`, `/etc/shadow` |
| `sshd` | `/etc/ssh/sshd_config` |
| `actions` | `/etc/sudoers`, `/etc/sudoers.d` |

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

**What it does:** Collects system performance data on a schedule (CPU, memory, I/O, network) and stores historical records. Not a security tool directly, but useful for spotting anomalies — e.g. unexpected I/O at 3 AM may indicate something scanning the disk.

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

| Jail | Trigger | Ban |
|------|---------|-----|
| `sshd` / `postfix` | 5 failures in 10 min (`maxretry 5`, `findtime 10m`) | 1 hour |
| `recidive` | 3 bans within 1 day | 7 days |

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

> A lingering `/var/run/reboot-required` (e.g. after a kernel update) is **intentional** — reboots are deferred to the weekly restart cron, not applied automatically.

---

## Quick Reference

| Concern | Command |
|---------|---------|
| Sensitive file modified | `sudo ausearch -k identity` or `-k sshd` |
| Rootkit / backdoor suspicion | `sudo rkhunter --check` |
| Unexpected file change | `sudo aide --check` |
| Unusual commands run as root | `sudo lastcomm` |
| Unusual I/O or CPU activity | `sar -d` / `sar -u` |
| Brute-force attempts / banned IPs | `sudo fail2ban-client status sshd` |
| Auto-patch history | `sudo grep Install /var/log/unattended-upgrades/unattended-upgrades.log` |
| Any security cron warning | `sudo grep -E 'rkhunter\|aide\|apt-autoremove\|dpkg-purge' /var/log/syslog` |
