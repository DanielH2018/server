### PR C-1a and C-1b

#### PR C-1a

[#7077](https://github.com/DanielH2018/server/pull/7077), from [#7061](https://github.com/DanielH2018/server/issues/7061), deletes
`KumaDown` with its inhibit, Prometheus's `uptime-kuma` scrape job and its credential,
`check_kuma_scrape`, the two `job="uptime-kuma"` rows in `files/rules/scrape-targets.yaml`, and
the Kuma read in `probe.py monitors`.

**The operator waived the Phase A week on 2026-11-03.** C-1a's precondition asked for a week of
Phase A with no Kuma page. The operator waived it as they waived the token week on 2026-11-02.
Every other precondition held, and these reads ran on 2026-11-03 at about 23:08Z.

The gate has two halves ([#7075](https://github.com/DanielH2018/server/issues/7075)). The instant
read returns the one tile:

```console
$ probe.py metric 'count by (monitor_type, monitor_name) (monitor_status)'
monitor_name=Status Page Sync, monitor_type=push = 1
```

`prod` holds `a4d5def5fa`, and the file declares one monitor at that commit:

```console
$ kubectl -n flux-system get ocirepository homelab \
    -o 'jsonpath={.status.artifact.metadata.org\.opencontainers\.image\.revision}'
master@sha1:a4d5def5fa1e3416f6db889c7e6dee589e811aff
$ git show a4d5def5fa:ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2 \
    | grep -E '\.json: \|$' | grep -vE '^ +(discord|email|tag-[a-z]+)\.json:'
  status-page-sync.json: |
```

**The triage poller has not read Kuma since B-10 installed.** Its last `uptime-kuma` line is
2026-11-03 at 00:50:25Z. A 7-day read is not clean, because the lines before that are the old
poller's. Since then the journal holds 667 `query=` lines and no `uptime-kuma` line. daniel-box
runs `homelab-daniel-box` `0.6730+g35edf86f1457`, whose commit descends from B-10's `589c932c85`.

**Homepage has no Kuma read left.** Its Kustomization reports the digest that holds `a4d5def5fa`,
and `git grep uptime-kuma a4d5def5fa -- ansible/roles/k8s/homepage` returns nothing.

**The cluster file does not change.** `cluster_vars.py check` reads clean, because
`uptime-kuma/templates/secret.yaml.j2` still renders `prometheus_kuma_api_key` for the maintenance
sync. C-1a therefore needs no operator `cluster_vars.py write`. C-5 retires the key.

**The render drops no object.** `prometheus-scrape-credentials` loses one key and stays, so the
PR carries no `Removes-Object:` line.

The two post-promotion reads follow once `promote` moves `prod` to a commit that holds C-1a. C-1b's
preconditions quote them again.

#### PR C-1b

C-1b empties the `uptime-kuma` render down to its two claims. The PR body quotes its
preconditions, its render and its `Removes-Object:` lines. The post-promotion read has not run:
it needs `promote` to move `prod` to a commit that holds C-1b.

**Files the plan does not list.** Each one pinned a deleted template, the deleted task, or a reader
that went with them:

- `uptime-kuma/defaults/main.yml` shrinks to the claims, because the renderer refuses
  `flux_prepull_timeout` on a render with no workload.
- `scripts/renderer/renderer_lib/task_inputs.py` drops `kuma_declarations`, and
  `secret_checks.py`'s rule 5 names a different member.
- `scripts/secrets_mgmt/consumers.py` no longer names `uptime-kuma` as a token's consumer, and
  `kuma_status_page_sync_push_token` moves from tier `auto` to `assisted` until C-5.
- `deploy_cross_role.py`'s k8s table loses `uptime-kuma` from the `kuma_push.py` row, which the
  plan gave to C-3a. The row's other two members stay for C-3a.
- `python-liquid` leaves the dev dependencies with the two tests that used it.
- `test_cron_scripts_publish_via_pr.py`: the push corpus falls to one template, because the
  Authelia bypass held the other `api/push` literal.
- `test_host_maintenance.py` takes over the reboot timer's assertion from
  `test_maintenance_window.py`, because the timer outlives the Kuma window.

**The plan's precondition grep names more than the plan predicted.** The other hits are comments,
the entry itself, C-2b's `k8s_host_files` task, C-7's `KumaNotificationDropped`, C-3a's push
library, an inert exclusion regex in `edge.yaml` and the disarmed `render_records` role. None
reads the entry's `port` or `hostname`, and the full cluster render passes.

**The prepull Kustomization leaves two objects behind.** The render drops
`flux/uptime-kuma-prepull.yaml`, so the root prunes that Kustomization under `deletionPolicy:
Orphan`. The Job `prepull-uptime-kuma-dd816639` and the ConfigMap `prepull-uptime-kuma-41b32c91`
stay in `flux-system`. The plan's post-promotion read does not see them. The operator deletes them
with the claims after C-2b.
