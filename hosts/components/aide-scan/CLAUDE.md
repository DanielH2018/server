# `hosts/components/aide-scan`

## Autonomous-role contract (the scan runs weekly with no human in the loop)

It reads the filesystem, reports, and advances its own baseline.

- **Scope / exclusions:** `aide --update` at idle CPU and IO priority, and the `aide-check` textfile
  in node_exporter's textfile directory. The check moves `aide.db.new` over `aide.db` after a run
  whose every difference a package explains. It never advances on a DOWN, and never
  creates an `aide.db` that is missing. Nothing reaches another host.
- **Mode (explicit and reversible):** the timer's enable link arms it. Dropping the component from a
  host package stops the timer on the install that drops it. A DOWN on an unexplained change
  stays DOWN until an operator reviews it and runs `/usr/lib/homelab/aide-check --accept`.
- **Authoritative sources:** AIDE's own exit code and report, and dpkg's file lists and digests; the
  script's header has why the exit code alone cannot say.
- **Abort valves:** a DOWN advances no baseline, and a missing `aide.db` stays missing.
- **Required evidence, every run:** an `aide-check` line in the journal and the textfile the
  `host-checks` rule group reads.
