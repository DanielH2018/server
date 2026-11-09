### PR C-4a and C-4b

#### PR C-4a

[#7121](https://github.com/DanielH2018/server/pull/7121), from
[#7115](https://github.com/DanielH2018/server/issues/7115), renames the `kuma:` key to `probe:` on
15 entries and feeds the `blackbox-ingress` job from `ingress_probe_targets`. Its base is
`936338336`, which is after C-2b.

**The render read.** The job's rendered targets match the base: `diff` exits 0, with 26 targets on
each side. The reworded job comment renders into `prometheus.yml`, so `checksum/config` moves and
the promotion rolls Prometheus once. The `probe_success` count after promotion has no recorded read
yet. To take it, run `count(probe_success{job="blackbox-ingress"})` and expect 26.

**The narrowing read.** `deploy_tags.py narrow` did not refuse. It names the 15 renamed entries'
own tags, `ingress_probes.py -> observability`, and this readers line:

```text
narrow: containers_list readers of kuma,probe on 15 entries -> longhorn-reconcile,observability via ansible/inventory/host_vars/daniel-box.yml
```

**`longhorn-reconcile` is a reader the plan does not predict.** Its `defaults/main.yml` calls
`tier_backup_claims`, `longhorn_weekly_claims` and `longhorn_nobackup_claims`, and none of them has
a `FILTER_FIELDS` row. `containers.reader_reads` therefore returns `None` for that file, so any
field edit reaches the tag. The plan's "No template reads a whole entry" does not cover a role's
defaults. [#7120](https://github.com/DanielH2018/server/issues/7120) holds the follow-up.

**`has_ingress_route` is a second reader the plan does not list.**
`scripts/diagnostics/probe_lib/health_alerts.py` read the opt-out through `kuma_ingress_monitors`.
On that filter it would return True for all 8 `probe: false` entries, so C-4a moved it too.

**The `id` and `name` overrides moved verbatim, and nothing reads them.**
[#7119](https://github.com/DanielH2018/server/issues/7119) holds their removal.

#### PR C-4b

[#7835](https://github.com/DanielH2018/server/issues/7835) deletes
`ansible/filter_plugins/kuma_monitors.py`, its import and its `kuma_ingress_monitors`
registration in `scripts/lib/ansible_jinja_env.py`. D6 had already deleted
`narrow_lib/containers.py`, so no `FILTER_FIELDS` row was left to remove. The plugin's other
filter, `kuma_malformed_push_tokens`, had no caller left, so it went with the file. The census
row's allowlist loses C-4b's two entries, which closes exit criterion 3's C-4b wait.
