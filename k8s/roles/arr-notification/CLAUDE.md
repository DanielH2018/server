# k8s/arr-notification — the shared *arr Discord Connect declaration

This role renders no manifest and starts no workload. It declares one *arr app's Discord
"Connect" notification over the app's own API
(`k8s/roles/arr-notification/files/seed_arr_notification.py:main`), so the notification is repo
state rather than UI state. The declaration runs as a seed Job that sonarr's, radarr's,
lidarr's and prowlarr's `templates/verify/notification.yaml.j2` render (*The Job form*, below). Each of those
templates reads this role's script and defaults by `lookup()`. Those lookups are the role's
only callers, and no app includes the role
(`k8s/roles/arr-notification/tests/test_seed_job.py::test_the_seed_job_reads_no_role_but_its_app_and_arr_notification`).

**Why it exists.** A Connect notification is a row in `sonarr.db` / `radarr.db` on the app's
config PVC. No manifest reproduces it, so a Longhorn revert past its creation, or anyone
recreating the claim, drops it silently — and the alert that goes missing is the health alert
that would have reported the problem.

**No standalone deploy tag.** Its callers reach it through a `lookup()`, never through
`--tags arr-notification`. It is not a `containers_list` entry, so a promoted image bump
here would match no play and deploy nothing while reporting success.
`k8s/roles/arr-notification/defaults/main.yml:k8s_autodeploy` is false for the same reason; the
reason string is `k8s/roles/arr-notification/defaults/main.yml:k8s_autodeploy_reason`.

## The before-state it adopts (measured 2026-09-06)

**HISTORY —** Both apps already carried a Discord notification named `Discord (health)`, created by hand:

| app | id | name | implementation | triggers true |
|---|---|---|---|---|
| sonarr | 3 | `Discord (health)` | Discord | `onHealthIssue`, `onHealthRestored` |
| radarr | 1 | `Discord (health)` | Discord | `onHealthIssue`, `onHealthRestored` |

**HISTORY —** Both point at the same webhook, and that webhook is byte-for-byte the SOPS
`arr_discord_webhook_url` that `autofix-bridge` and the `fake-remux` component already read. So this role
adopted two rows and minted nothing: the first deploy reported **no change**.

**HISTORY —** lidarr (added in #8365) had no such row. Its first deploy was to **create**
`Discord (health)` from `GET /api/v1/notification/schema`, with the same health-only triggers.
On 2026-11-14 lidarr's seed Job reported **no change**, so the row exists.

**HISTORY —** prowlarr carried a hand-made `Discord (health)` row whose webhook Discord
answered with a 404 on 2026-11-21, while the SOPS webhook still passed monitor-bridge's
`discord` check (#9565). Its seed Job adopts that row by name and writes the SOPS webhook
into it.

A missing row is created from the app's schema
(`k8s/roles/arr-notification/files/seed_arr_notification.py:schema_for`), and a row someone adds
by hand under the declared name first is adopted instead.

**HISTORY —** sonarr carried an `Episode Trimmer` CustomScript notification (id 2,
`onDownload` + `onUpgrade`) that ran `/config/sonarr-episode-trimmer.py`, a file the pod never
had. The operator deleted the row in the UI on 2026-11-22 (#9737).

`find_notification` matches on implementation AND name, so a CustomScript that shares the
declared name is left alone
(`k8s/roles/arr-notification/files/seed_arr_notification.py:find_notification`,
`k8s/roles/arr-notification/tests/test_seed_arr_notification.py::test_a_same_named_notification_of_another_kind_is_not_adopted`).

## Decisions worth not re-opening

- **Health-only triggers.** Issue #1377 asked for on-grab, on-import and on-application-update
  as well. That is a behaviour change, not a declaration — every episode and every movie import
  would post to the channel. `arr_notification_triggers` in `defaults/main.yml` carries the
  `# DECIDED:` marker and the argument; widening is an edit to that one list.
- **It adopts, it never deletes.** A Discord notification this role does not match is left
  alone. Deleting on a name miss would destroy live rows from a deploy path.
- **The API version is per app, inside the script.** `ARR_API_VERSIONS` maps sonarr and radarr
  to `v3` and lidarr and prowlarr to `v1`, keyed like `ARR_PORTS`, so no caller passes it. Lidarr's
  notification carries `onHealthIssue`, `onHealthRestored` and `includeHealthWarnings` under
  the same names (Lidarr v3.1.6.5079, `NotificationResource`), and so does Prowlarr's (Prowlarr
  `develop` on 2026-11-21, `Prowlarr.Api.V1` `NotificationResource`), so the health-only
  default applies unchanged. Prowlarr's `onGrab` and `onApplicationUpdate` are set false.
- **Every trigger the app carries is set, not just the true ones.** `trigger_keys` derives the
  set from the object the API returned, because the apps do not carry the same keys
  and upstream adds more. Declaring only the true half would let a trigger enabled in the UI
  survive a deploy that is meant to be the whole truth about when Discord is notified.
- **The update merges into the body the API returned.** Building one from scratch blanks
  `grabFields` / `importFields` / `manualInteractionFields`, which the app then repopulates
  empty — content loss from a task that meant to change two booleans.
- **Never through Traefik.** Neither app has an Authelia bypass for `/api/*`, so a routed
  request 302s at the middleware without reaching the app. The seed Job runs inside the
  cluster, so it dials the app's Service by its cluster name,
  `<app>.homelab.svc.cluster.local`. A hand run from outside the cluster cannot resolve that
  name, so it sets `ARR_HOST` to the Service's ClusterIP, the same workaround as
  `scripts/diagnostics/probe_lib/arr.py`.
- **Secrets arrive in the environment, and the script never prints a field value.** The Job
  fills `ARR_API_KEY` and `ARR_DISCORD_WEBHOOK_URL` from two Secrets, and a hand run sets them
  in the operator's shell; neither passes them on argv. The script's output is one line naming
  the decision (created, updated or no change). The Job log can therefore be read in full,
  failures included.

## Verifying it by hand

`probe.py arr <app> notification` **prints the webhook URL in full**. Filter it:

```bash
uv run python scripts/diagnostics/probe.py arr sonarr notification --json \
  | jq -c '[.[]|{id,name,implementation,on:[to_entries[]|select((.key|startswith("on")) and (.value==true))|.key]}]'
```

To make the app post a real Discord message through the resolved spec, run the seed with
`--test` — same env contract, and it changes nothing:

```
ARR_APP=… ARR_HOST=<clusterIP> ARR_API_KEY=… ARR_DISCORD_WEBHOOK_URL=… \
ARR_NOTIFICATION_NAME='Discord (health)' ARR_NOTIFICATION_USERNAME=Sonarr \
ARR_NOTIFICATION_TRIGGERS='["onHealthIssue","onHealthRestored"]' \
ARR_NOTIFICATION_INCLUDE_HEALTH_WARNINGS=false \
PYTHONPATH=scripts/renderer/job_files \
uv run python k8s/roles/arr-notification/files/seed_arr_notification.py --test
```

`PYTHONPATH` is there because the script opens every connection through
`flux_job_connect.py`, which lives in `scripts/renderer/job_files/`.

## The Job form

The seed runs as a post-apply mutation Job in `sonarr-verify`, `radarr-verify`,
`lidarr-verify` and `prowlarr-verify`. The Job's body is one macro, `k8s/templates/arr-notification-seed.yml.j2`.
Each app's `templates/verify/notification.yaml.j2` calls it, byte-identical in all four
(`k8s/roles/arr-notification/tests/test_seed_job.py::test_the_apps_call_the_one_shared_seed_job`).
The caller passes in every `lookup()` the macro needs. The two lookups into this role must stay
in the caller, because `scripts/lib/k8s_role_callers.py:role_callers` reads only a role's own
templates, and those lookups are each app's only edge to this role
(`scripts/lib/tests/k8s/test_k8s_role_callers.py::test_the_lookup_form_resolves`).

- **The script is the one this role ships**, read by a cross-role `lookup()`, so a change here
  changes all four apps' renders. The template also reads
  `k8s/roles/arr-notification/defaults/main.yml:arr_notification_name`,
  `k8s/roles/arr-notification/defaults/main.yml:arr_notification_triggers` and
  `k8s/roles/arr-notification/defaults/main.yml:arr_notification_include_health_warnings` from
  `defaults/main.yml` as literals, and spells
  `k8s/roles/arr-notification/defaults/main.yml:arr_notification_username` as the app's name
  capitalised
  (`k8s/roles/arr-notification/tests/test_seed_job.py::test_the_job_declares_what_the_ansible_seed_declares`).
- **One transport.** The script imports only modules the Job census admits, so a hand run
  reaches the app through `scripts/renderer/job_files/flux_job_connect.py:open_url` as the Job
  does. A refused connect therefore retries for 60 seconds in a hand run too
  (`scripts/renderer/job_files/flux_job_connect.py:RETRY_SECONDS`).
- **The API key comes from `<app>-exportarr`**, which the app's own render applies. The webhook
  comes from `<app>-notification`, a Secret in `<app>-verify` filled by substitution, so that
  Kustomization substitutes
  (`k8s/roles/arr-notification/tests/test_seed_job.py::test_every_secret_the_job_reads_is_rendered_before_it_runs`).
- **The Job reruns once per revision that rolls the app**, because its spec embeds a digest of
  the rendered Deployment (`ARR_DEPLOYMENT_DIGEST8` in
  `k8s/templates/arr-notification-seed.yml.j2`).
- **The Job also reruns when `arr_discord_webhook_url` rotates.** Its spec embeds the
  `cluster-vars.source` digest of that key (`ARR_WEBHOOK_DIGEST8`), so a rotation alone renames
  the Job and the seed writes the new URL into each app's database (#8602). The rendered
  `<app>-notification` Secret cannot carry the rotation, because its placeholder text does not
  move when the key does. `tests/test_seed_job.py` rotates the key in a tree copy and checks
  that each app's Job is renamed
  (`k8s/roles/arr-notification/tests/test_seed_job.py::test_a_webhook_rotation_alone_renames_the_seed_job`).
- **The fence peers are in place.** The Job's pod carries `app: <app>-verify`, and
  each app's `netpol_from` lists that label (#5535). `tests/test_seed_job.py` checks that the
  Job's pod is one its app's fence admits
  (`k8s/roles/arr-notification/tests/test_seed_job.py::test_the_apps_fence_admits_every_flipped_job`).

## Tests

`k8s/roles/arr-notification/tests/test_seed_arr_notification.py`, registered in
`pyproject.toml`'s `testpaths`. Every rule is an accept/reject pair over
`k8s/roles/arr-notification/files/seed_arr_notification.py:find_notification` /
`k8s/roles/arr-notification/files/seed_arr_notification.py:declared_body` /
`k8s/roles/arr-notification/files/seed_arr_notification.py:needs_update` —
the three functions that decide create, rewrite or no-op.

`tests/test_seed_job.py` renders the four apps' Jobs, runs the Job census over them, and
runs the rendered script twice against a fixture API from no row and from a drifted row. The
second run must write nothing; the Job's `homelab/rerun-safe` annotation names that test
(`k8s/roles/arr-notification/tests/test_seed_job.py::test_a_second_run_changes_nothing`). A
red-proof pair seeds lidarr against a fixture that serves only `/api/v1` (accepted) and only
`/api/v3` (refused)
(`k8s/roles/arr-notification/tests/test_seed_job.py::test_lidarr_seeds_over_api_v1_and_only_v1`).

**HISTORY — The create path is unexercised live until lidarr's first deploy.** The sonarr and radarr
notifications already existed when this role was written, so only the update-and-no-op branch
has run against those apps. Lidarr's first deploy is the first live create; before it, the
create branch is covered by the schema test and the fixture run alone.
