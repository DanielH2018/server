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
