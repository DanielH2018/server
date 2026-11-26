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
- **Claim:** `tft-assistant-data` (no backup (StorageClass longhorn-nobackup))
<!-- /generated_from -->

## Notable

- **The route must stay behind Authelia.** The crawler runs on a personal Riot API key, and
  Riot's terms forbid public use of an app on one. Do not add a public bypass to the
  `tft-assistant` entry in `k8s/services.yml`.
- **The web container holds no key.** Only the crawler container reads the Secret
  (`k8s/roles/tft-assistant/templates/secret.yaml.j2`). The web container mounts the volume
  read-only and runs Python's `http.server` over the built page.
- **The volume is not backed up, on purpose.** Its storage class is
  `k8s/roles/tft-assistant/defaults/main.yml:tft_assistant_k8s_storage_class`. Every byte
  comes back from Riot's API and Community Dragon. After a lost volume the pod crawls again,
  and the page has data within its first hour.
- **A dead key shows as a restarting crawler and a page that goes stale.** A failed crawl
  ends the crawler process, the kubelet restarts it, and `K8sContainerCrashLooping` fires
  (`k8s/roles/observability/files/rules/k8s-workloads.yaml`). The web container keeps serving
  the last page, whose footer states the age of its newest match. A personal key does not
  expire on a schedule. A development key expires after 24 hours, so a Secret that holds one
  needs the rotation under *Editing* every day.
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
- **Writes, and nothing else.** Files on its own volume: the match store, a cache of
  Community Dragon's static data, and the built page. It writes nothing in the cluster and
  has no use for the Kubernetes API.
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
