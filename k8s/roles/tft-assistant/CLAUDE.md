# tft-assistant — Teamfight Tactics comp reference, built from crawled ranked matches

One pod with two containers from one image. The crawler stores ranked matches from Riot's API
and builds one static page from them once an hour. The web container serves that page. The
image and its source are [`DanielH2018/tft-assistant`](https://github.com/DanielH2018/tft-assistant).
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=tft-assistant` gates the rollout
- **Image:** `ghcr.io/danielh2018/tft-assistant` (`tft_assistant_k8s_image`)
- **Route:** `tft.<domain>` · `tft.local.<domain>`, Authelia one_factor
- **Claims:** `tft-assistant-data` (no backup (StorageClass longhorn-nobackup)),
  `tft-assistant-keep` (weekly -> B2 (default target))
<!-- /generated_from -->

## Notable

- **The route must stay behind Authelia.** The crawler runs on a personal Riot API key, and
  Riot's terms forbid public use of an app on one. Do not add a public bypass to the
  `tft-assistant` entry in `k8s/services.yml`.
- **The web container holds no key.** Only the crawler container reads the Secret
  (`k8s/roles/tft-assistant/templates/secret.yaml.j2`). The web container runs the image's
  `serve` command: the built page, and an API over the notes a reader writes on it. It
  mounts the data volume read-only and writes the kept volume only.
- **The data volume is not backed up, on purpose.** Its storage class is
  `k8s/roles/tft-assistant/defaults/main.yml:tft_assistant_k8s_storage_class`. Every byte
  comes back from Riot's API and Community Dragon. After a lost volume the pod crawls again,
  and the page has data within its first hour.
- **The kept volume is backed up weekly.** It holds what no API gives back: the notes, and the comp
  ids the notes are keyed by. Its storage class is
  `k8s/roles/tft-assistant/defaults/main.yml:tft_assistant_k8s_keep_storage_class`. The
  crawler writes the comp ids at `/data/keep`, and the web container writes the notes at
  `/state/keep`, which is the same claim.
  `k8s/roles/tft-assistant/templates/deployment.yaml.j2` says why the two paths differ.
- **The API checks nobody's identity.** Authelia is the only thing between the route and a
  write to the notes, which is a second reason the route keeps its login.
- **A dead key shows as a page that goes stale, and nothing restarts.** From image 0.2.7 a
  failed crawl prints `the crawl failed and the next cycle tries again` in the crawler's log
  and the crawler stays up. The web container keeps serving the last page, whose footer
  states the age of its newest match, and no alert fires. A personal key does not expire on
  a schedule. A development key expires after 24 hours, so a Secret that holds one needs the
  rotation under *Editing* every day.
- **HISTORY — a dead key took the page down on 2026-11-27.** Before image 0.2.7 a failed
  crawl ended the crawler process. The container restarted in a loop, the pod read 1/2, and
  Traefik answered 503 for the page although the Service publishes unready addresses: the
  endpoint read `ready=true serving=false`, and Traefik routed nothing to it. A crawler
  that exits takes the page with it, so the crawler must not exit over a condition that
  outlasts a restart.
- **The page keeps its endpoint while the pod is unready.**
  `k8s/roles/tft-assistant/templates/service.yaml.j2:DECIDED: the page keeps its endpoint while the pod is unready`
  holds the reason. The role ships its own Service for that one field.
- **The pod is pinned to one node.**
  `k8s/roles/tft-assistant/templates/deployment.yaml.j2:DECIDED: pinned to the node with memory to spare`
  holds the reason, and
  `k8s/roles/tft-assistant/defaults/main.yml:tft_assistant_k8s_node` names the node.
- **A new patch needs a release of the image.** The image carries each set's patch calendar.
  The image's repository publishes on a version tag, and Renovate offers the bump to the pin
  in `k8s/roles/tft-assistant/defaults/main.yml`, key `tft_assistant_k8s_image`.

## Autonomous-role contract

The crawler changes state with no human in the loop, so its authority is written here to
keep a later edit from widening it.

- **Schedule.** One cycle an hour, started by the process itself. No CronJob runs.
- **Writes, and nothing else.** Files on the role's two volumes: the match store, a cache of
  Community Dragon's static data and the built page on the data volume, and the comp ids on
  the kept volume. It never writes the notes. It writes nothing in the cluster and has no
  use for the Kubernetes API.
- **Calls out.** Riot's API, on the `americas`, `europe` and `asia` routing hosts and the
  `na1`, `euw1` and `kr` platform hosts, and `raw.communitydragon.org`. It stays under the
  key's rate limit of 100 requests per two minutes per routing host.
- **Reads.** One Secret key, `RIOT_API_KEY`.

## Editing

- Manifests: `k8s/roles/tft-assistant/templates/`. The IngressRoute renders from
  `k8s/templates/ingressroute-default.yaml.j2`, off the `containers_list` entry.
- Image pin, Renovate stance, resources, node, uid and the claim:
  `k8s/roles/tft-assistant/defaults/main.yml`.
- Rotating the key: `sops set` of `riot_api_key`, then `cluster_vars.py write`, committed
  together. Both are operator-only.
