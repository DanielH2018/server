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
