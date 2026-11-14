# k8s/arr-notification — the shared *arr Discord Connect declaration

This role renders no manifest and starts no workload. It declares one *arr app's Discord
"Connect" notification over the app's own API, so the notification is repo state rather than
UI state. The declaration runs as a seed Job in sonarr's, radarr's and lidarr's
`templates/verify/` (*The Job form*, below), which reads this role's script and defaults by
`lookup()`. Those lookups are the role's only callers, and no app includes the role.

**Why it exists.** A Connect notification is a row in `sonarr.db` / `radarr.db` on the app's
config PVC. No manifest reproduces it, so a Longhorn revert past its creation, or anyone
recreating the claim, drops it silently — and the alert that goes missing is the health alert
that would have reported the problem.

**No standalone deploy tag.** Its callers reach it through a `lookup()`, never through
`--tags arr-notification`. It is not a `containers_list` entry, so a promoted image bump
here would match no play and deploy nothing while reporting success. `k8s_autodeploy: false`
for the same reason; the reason string is in `defaults/main.yml`.

## The before-state it adopts (measured 2026-09-06)

Both apps already carried a Discord notification named `Discord (health)`, created by hand:

| app | id | name | implementation | triggers true |
|---|---|---|---|---|
| sonarr | 3 | `Discord (health)` | Discord | `onHealthIssue`, `onHealthRestored` |
| radarr | 1 | `Discord (health)` | Discord | `onHealthIssue`, `onHealthRestored` |

Both point at the same webhook, and that webhook is byte-for-byte the SOPS
`arr_discord_webhook_url` that `autofix-bridge` and the `fake-remux` component already read. So this role
adopts two rows and mints nothing: the first deploy reports **no change**.

lidarr (added in #8365) had no such row. Its first deploy **creates** `Discord (health)` from
`GET /api/v1/notification/schema`, with the same health-only triggers; a row someone adds by
hand under that name first is adopted instead.

sonarr also carries an `Episode Trimmer` CustomScript notification (id 2, `onDownload` +
`onUpgrade`). This role never touches it — `find_notification` matches on implementation AND
name, so a CustomScript that happened to share the name is left alone.

## Decisions worth not re-opening

- **Health-only triggers.** Issue #1377 asked for on-grab, on-import and on-application-update
  as well. That is a behaviour change, not a declaration — every episode and every movie import
  would post to the channel. `arr_notification_triggers` in `defaults/main.yml` carries the
  `# DECIDED:` marker and the argument; widening is an edit to that one list.
- **It adopts, it never deletes.** A Discord notification this role does not match is left
  alone. Deleting on a name miss would destroy live rows from a deploy path.
- **The API version is per app, inside the script.** `ARR_API_VERSIONS` maps sonarr and radarr
  to `v3` and lidarr to `v1`, keyed like `ARR_PORTS`, so no caller passes it. Lidarr's
  notification carries `onHealthIssue`, `onHealthRestored` and `includeHealthWarnings` under
  the same names (Lidarr v3.1.6.5079, `NotificationResource`), so the health-only default
  applies unchanged.
- **Every trigger the app carries is set, not just the true ones.** `trigger_keys` derives the
  set from the object the API returned, because the three apps do not carry the same keys
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

The seed runs as a post-apply mutation Job in `sonarr-verify`, `radarr-verify` and
`lidarr-verify`. `templates/verify/notification.yaml.j2` in each app's role holds it,
byte-identical in all three.

- **The script is the one this role ships**, read by a cross-role `lookup()`, so a change here
  changes both apps' renders. The template also reads
  `arr_notification_name`, `arr_notification_triggers` and
  `arr_notification_include_health_warnings` from `defaults/main.yml` as literals, and spells
  `arr_notification_username` as the app's name capitalised.
- **One transport.** The script imports only modules the Job census admits, so a hand run
  reaches the app through `flux_job_connect.open_url` as the Job does. A refused connect
  therefore retries for 60 seconds in a hand run too.
- **The API key comes from `<app>-exportarr`**, which the app's own render applies. The webhook
  comes from `<app>-notification`, a Secret in `<app>-verify` filled by substitution, so that
  Kustomization substitutes.
- **The Job reruns once per revision that rolls the app**, because its spec embeds a digest of
  the rendered Deployment. A webhook rotation alone renames nothing, so the rotated URL reaches
  the app on the next revision that rolls it.
- **The fence peers are in place.** The Job's pod carries `app: <app>-verify`, and
  each app's `netpol_from` lists that label (#5535). `tests/test_seed_job.py` checks that the
  Job's pod is one its app's fence admits.

## Tests

`tests/test_seed_arr_notification.py`, registered in `pyproject.toml`'s `testpaths`. Every
rule is an accept/reject pair over `find_notification` / `declared_body` / `needs_update` —
the three functions that decide create, rewrite or no-op.

`tests/test_seed_job.py` renders the three apps' Jobs, runs the Job census over them, and
runs the rendered script twice against a fixture API from no row and from a drifted row. The
second run must write nothing; the Job's `homelab/rerun-safe` annotation names that test. A
red-proof pair seeds lidarr against a fixture that serves only `/api/v1` (accepted) and only
`/api/v3` (refused).

**The create path is unexercised live until lidarr's first deploy.** The sonarr and radarr
notifications already existed when this role was written, so only the update-and-no-op branch
has run against those apps. Lidarr's first deploy is the first live create; before it, the
create branch is covered by the schema test and the fixture run alone.
