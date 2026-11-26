# `hosts/components/aide-scan`

## Autonomous-role contract (the scan runs weekly with no human in the loop)

It reads the filesystem, reports, and advances its own baseline.

- **Scope / exclusions:** `aide --update` at idle CPU and IO priority, and the `aide-check` textfile
  in node_exporter's textfile directory. The check moves `aide.db.new` over `aide.db` after a run
  whose every difference a package explains. It never advances on a DOWN, and never
  creates an `aide.db` that is missing. Its one network call, before the scan, is `apt-get
  download` from the configured archive, for the package copy of a conffile the lines arm makes
  line edits to when no copy of the installed version is kept in `/var/lib/homelab-aide-originals`.
  The classifier reads only the kept copies, so a failed fetch with no older version's copy kept
  leaves that file "original unavailable" and pages it, never skips it. A run deletes only a
  kept copy of the installed version whose md5 is not dpkg's digest, then fetches it again.
- **Recovery when no run kept the old copy:** an upgrade before the component's first run on a
  host leaves an edited conffile built on a copy no run can fetch, and it pages as `differs`.
  To clear it, move the file aside, run `sudo apt-get install --reinstall -o
  Dpkg::Options::=--force-confmiss <package>`, and let the next reconcile reapply the lines
  arm's edits. `<package>` is the owner `dpkg -S <file>` names, which can differ from the
  package the file is named after: on Ubuntu 26.04 `login.defs` owns `/etc/login.defs`, not
  `login`. While the file is missing, the lines arm refuses to edit it rather than create a
  file holding only its managed lines
  (`hosts/lib/tests/reconcile/test_reconcile_lines_arm.py::test_a_missing_package_owned_conffile_is_refused_and_not_created`).
- **Mode (explicit and reversible):** the timer's enable link arms it. Dropping the component from a
  host package stops the timer on the install that drops it. The component also ships
  `/etc/rkhunter.d/homelab-aide-owns-accounts.conf`, which turns off rkhunter's
  `passwd_changes` and `group_changes` tests, because this check judges account changes. The
  file is a plain package file, not a conffile, so dropping the component removes it and
  rkhunter's account tests come back on that host. A DOWN on an unexplained change
  stays DOWN until an operator reviews it and runs `/usr/lib/homelab/aide-check --accept`.
- **Authoritative sources:** AIDE's own exit code and report, dpkg's file lists and digests, and
  the kept package copies, each checked against dpkg's digest on every run while its version is installed. An edit replayed
  on a copy of any kept version explains the file, because under `--force-confold` dpkg keeps an
  edited conffile built on the old version's copy across an upgrade
  (`hosts/components/aide-scan/files/aide_originals.py:DECIDED: a kept copy of any version`).
  The script's header has why the exit code alone cannot say.
- **Abort valves:** a DOWN advances no baseline, and a missing `aide.db` stays missing.
- **Required evidence, every run:** an `aide-check` line in the journal and the textfile the
  `host-checks` rule group reads.
