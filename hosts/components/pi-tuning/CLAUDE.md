# `hosts/components/pi-tuning`

## Autonomous-role contract (homelab-log2ram-hold re-applies an apt hold with no human in the loop)

- **Scope:** the dpkg hold on the `log2ram` package on daniel-pi. Nothing else: the unit never
  upgrades, installs or removes a package.
- **Mode:** the enable links in this package arm the service at boot and the timer daily at
  05:05 UTC. Dropping both links from a package version, or reverting this component, stops the
  re-hold on the install that drops them. The hold itself stays until an operator lifts it.
- **Authoritative sources:** `apt-mark showhold log2ram`, and whether dpkg lists log2ram as
  installed (`/var/lib/dpkg/info/log2ram.list`).
- **Abort valves:** a Pi without log2ram skips the unit, and a package that is already held skips
  the `apt-mark hold` call, so the steady state changes nothing and takes no dpkg lock.
- **Required evidence:** `journalctl -u homelab-log2ram-hold` on daniel-pi, and
  `apt-mark showhold` listing `log2ram`. An unplanned upgrade that got through still pages as
  `PiSdHealthFailed`, because `/var/log` stops being a tmpfs.
- **Next-run review:** a failed run means `apt-mark` could not take the dpkg lock within 60
  seconds. The next boot or 05:05 run retries it. `docs/pi-host-tuning-record.md` has the
  planned-upgrade steps that lift the hold.

## Autonomous-role contract (log2ram syncs /var/log to the card before each unattended upgrade)

- **Scope:** one log2ram write, `systemctl reload log2ram.service`, as the first step of
  `apt-daily-upgrade.service` on daniel-pi. It copies the `/var/log` tmpfs to
  `/var/hdd.log` on the SD card and rotates the journal, as log2ram-daily's 23:55 write does.
- **Mode:** the `apt-daily-upgrade.service` drop-in arms it on every upgrade run, at 05:20 UTC
  plus up to 10 minutes. Dropping the drop-in from a package version leaves only the 23:55
  write.
- **Authoritative sources:** `/var/log/log2ram.log`, which gains one sync before each upgrade
  run, and the mtime of `/var/hdd.log/log2ram.log`.
- **Abort valves:** the `-` prefix lets the upgrade run when the write fails, as it does while
  log2ram.service is inactive.
- **Required evidence:** `journalctl -u apt-daily-upgrade` on daniel-pi shows the reload before
  the upgrade, and after a reset `/var/log/apt/history.log` keeps the entries up to that run.
- **Next-run review:** a failed write leaves a non-zero ExecStartPre line in that journal; the
  next morning's run and the 23:55 write retry it. Each write adds a journal archive, so
  `60-homelab-pi.conf` sizes `SystemMaxFiles` for five a day.

## Autonomous-role contract (logrotate runs hourly on daniel-pi)

- **Scope:** Ubuntu's own `logrotate.service` over `/etc/logrotate.conf`, on daniel-pi only.
  The `logrotate.timer` drop-in changes when it runs, never what it rotates.
- **Mode:** the drop-in replaces the packaged daily schedule with `OnCalendar=hourly`.
  Dropping the drop-in from a package version restores the daily run on the install that drops
  it.
- **Abort valves:** a `daily`, `weekly` or `monthly` stanza still rotates once per interval, by
  its date in `/var/lib/logrotate/status`. Only a `size` stanza can rotate on more than one run
  a day, and only past its cap.
- **Required evidence:** `journalctl -u logrotate` on daniel-pi, and the dates of
  `/var/log/account/pacct.0` and `pacct.1.gz`, which sit about nine hours apart.
- **Next-run review:** a failed run names the stanza in its journal line. A duplicate stanza for
  one log fails the run, so check `/etc/logrotate.d` for one after a package upgrade.
