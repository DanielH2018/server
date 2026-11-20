# `hosts/components/integrity-scans`

## Autonomous-role contract (the scan runs weekly with no human in the loop, and the baseline refresh runs after every dpkg run)

The scan reads the filesystem and reports. The refresh advances rkhunter's baseline, outside apt's lock, by the same `rkhunter --propupd` that rkhunter's APT_AUTOGEN hook would run inside it.

- **Scope / exclusions:** `rkhunter --check` and `rkhunter --propupd` at idle CPU and IO priority,
  and the `rkhunter-check` textfile in node_exporter's textfile directory. Nothing reaches
  another host.
- **Mode (explicit and reversible):** the timer's enable link arms it. Dropping the component from a
  host package stops the timer on the install that drops it.
- **Authoritative sources:** rkhunter's log summary; the script's header has why the exit code
  alone cannot say.
- **Abort valves:** the scan advances no baseline.
- **Required evidence, every run:** an `rkhunter-check` line in the journal and the textfile the
  `host-checks` rule group reads. Each refresh logs a `rkhunter baseline refreshed`
  line under the `rkhunter-propupd` tag, and each skipped one a `rkhunter baseline kept` line.
