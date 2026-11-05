### PR C-5

Tracked by [#7173](https://github.com/DanielH2018/server/issues/7173). C-5 retires four keys:
`kuma_status_page_sync_push_token`, `prometheus_kuma_api_key`, `uptime_kuma_password` and
`uptime_kuma_username`. A `git grep` on 2026-11-05 found no reader of any of them. `secretq keys`
on the same day found none of them in a host SOPS file or the cluster file, so the operator's
`cluster_vars.py write` and `host_secrets.py sync` are expected to change nothing, and the PR waits
on no host package.

The code half adds the four names to `git_dates.RETIRED`. It deletes
`sops_io.malformed_push_tokens` and `cluster_vars.py`'s `_push_token` refusal, as the plan assigns,
and `rotation_tools.decrypted_values`, because `audit`'s shape check was its only caller. `audit`
therefore no longer decrypts anything.

`render_records_push_token` stays. `ansible/roles/setup/render_records/templates/config.env.j2`
still renders it, and `consumers.py:CROSS_HOST_PUSH_TOKENS` still names it. R4 owns its removal,
in the secrets commit that follows its role deletion (`docs/target-architecture/render/r4-delete-k8s-ansible-path.md`,
*The push token leaves after the role*). After C-5, `grep -c '_push_token:'` prints 1 for both
`ansible/secret_rotation.yml` and `ansible/vars/secrets.yml`, so exit criterion 2 holds only once
R4's commit lands.
