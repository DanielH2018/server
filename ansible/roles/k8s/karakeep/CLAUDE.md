# karakeep — bookmark archiving, with a search index and an AI tagger

Karakeep (the app), a `karakeep-chrome` headless-shell sidecar for page snapshots, a
Meilisearch Deployment for search, and a `time-tagger` sidecar that calls the app's API on a
loop to auto-tag bookmarks.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=karakeep` with exit 5
- **Images:** `ghcr.io/karakeep-app/karakeep` (`karakeep_k8s_image`),
  `ghcr.io/karakeep-app/karakeep-chrome` (`karakeep_k8s_chrome_image`), `getmeili/meilisearch`
  (`karakeep_k8s_meili_image`), `<k8s_registry_pull_host>/karakeep`
  (`karakeep_k8s_tagger_image`)
- **Route:** `karakeep.<domain>` · `karakeep.local.<domain>`, Authelia one_factor
- **Claims:** `karakeep-meili` (no backup (StorageClass longhorn-nobackup)), `karakeep-data`
  (weekly -> B2 (default target))
<!-- /generated_from -->

- **Flux applies it, not Ansible** (#5652, Wave C). Its `containers_list` entry carries
  `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it. A merged change
  reaches the cluster when `promote` publishes and verifies the next bundle. To give it back
  to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of the
  flag leaves two writers on the objects. `k8s_autodeploy: false` still stops Renovate's
  automerge of a pin bump.
- **Three Kustomizations.** `karakeep-snapshot` snapshots `karakeep-data` before `karakeep`
  applies, so the stateful hold (D5.2b1) covers every revision that moves it. `karakeep`
  waits up to its 1200s `manifests_rollout_timeout`. `karakeep-verify` then runs the archive
  pruner's gate (below). `tasks/main.yml` keeps the image-builder and manifests includes for
  a handback, and no Flux path runs them.
- **The Authelia bypass:** `/api/v1/`, `/api/trpc/` and `/api/assets`, deliberately public (Bearer-token auth; the
  browser extension and mobile app can't pass 2FA, and they speak tRPC, not the REST API).
  `/api/auth/*` (next-auth's password login) stays behind Authelia on both names: the bypass
  was `/api/` until #1929, which put the app password alone in front of it on the internet,
  and the `.local` monitoring route (homepage's widget, ClientIP-gated to the bridge IP and
  the pod CIDR) carried the same `/api/` until #2018. It is now `/api/v1/users/me/stats`, the
  one path the widget reads. Reading the block above, "Authelia" means the UI; the bypass
  list is what a client without a browser can reach.
- **Persists:** `karakeep-data` (`longhorn`, backed up, ~487M) — bookmark library, page
  snapshots, `db.db`. `karakeep-meili` (`longhorn-nobackup`, ~286M) — the search index,
  deliberately unseeded and unbacked-up: it's rebuildable from `db.db` by reindexing.
- **Secrets (SOPS keys, not values):** `karakeep_meili_master_key`, `karakeep_gemini_api_key`
  (as `OPENAI_API_KEY`), `karakeep_python_api_key` (the tagger's).

## Notable
- **The two backend NetworkPolicies ship in THIS role, not in netpol-baseline** (#1620).
  `deployment.yaml.j2`'s `wait-for-deps` initContainer retries `karakeep-chrome:9222` and
  `karakeep-meilisearch:7700` in an unbounded loop, and the baseline admits neither path, so a
  policy one role away meant `--tags karakeep` could stage a pod that sat in Init forever with
  no restart count to read. karakeep itself only DEGRADES without meilisearch — it logs search
  errors and serves — so the warrant is the init gate rather than the app, which is the one
  difference from authelia's session store (#1609, where the app exits).
  `ansible/tests/services/test_karakeep_backend_policies.py` pins the co-location, the ports and
  the two files' place in the role's resolved `manifests_files`. Each port and caller list is a `netpol_fences` item with
  `role_owned: true` on this role's containers_list entry, and each template renders its item
  through the `networkpolicy` macro. The policies carry no `netpol_baseline_enforced` branch: that
  lever is a netpol-baseline role default and role defaults are role-scoped, so it does not
  resolve here.
- `manifests_extra_rollouts` rolls `karakeep-meilisearch` and `karakeep-time-tagger` on every
  manifest change, not just a Secret change — `MEILI_MASTER_KEY` and
  `KARAKEEP_PYTHON_API_KEY` are env vars, injected once at container start, so a key rotation
  that doesn't also roll these two sidecars leaves them on the old value while `karakeep`
  itself gets the new one. Under Flux the renderer reads the same list and stamps its restart
  checksum annotation on `karakeep`, `karakeep-meilisearch` and `karakeep-time-tagger`, so a
  change to the role's render rolls all three.
- `karakeep-chrome` is listed too, with `restart_on: []` (#4564). The drain waits on the roll
  a `karakeep_k8s_chrome_image` bump makes, and no karakeep edit restarts chrome mid-crawl.
  The renderer honours the empty list, so chrome gets no restart checksum.
  `ansible/tests/services/test_karakeep_chrome_rollout.py` pins both.
- **The time-tagger's image is built in-cluster**, from `templates/Dockerfile.j2`, since #2672
  — one `chown` layer over the pinned `ghcr.io/astral-sh/uv` digest, plus `UV_CACHE_DIR=/uv-cache`.
  It exists only so the tagger can run as uid 1000. Two paths blocked that from the manifest
  side, and neither was reachable with a securityContext: `uv pip install --system` writes the
  image's own site-packages on every container start, and the uv cache was mounted at
  `/root/.cache/uv`, behind Debian's 0700 `/root`. The relocation is why the mount path and
  `UV_CACHE_DIR` have to move together. The base digest moved out of `defaults/main.yml` into
  that `FROM` line, where Renovate's dockerfile manager reads it. A merged bump moves the
  content tag, and promote's `images` job builds `ghcr.io/danielh2018/karakeep` and rolls the
  pods that run it. `renovate.json`'s denylist-marker rule lists the file because this role
  is `k8s_autodeploy: false`. The glance still shows `<k8s_registry_pull_host>`, because it
  reads the defaults; the renderer rewrites the ref to GHCR with `imagePullPolicy:
  IfNotPresent`. The image is 91.3 MB in six layers. On 2026-10-18 its largest layer
  (28.2 MB) pulled from GHCR at 0.32-0.34 MB/s, so a cold pull takes about 270-290s, inside
  the 1200s budget. `karakeep` is `Recreate`, so the pod is down for that pull whenever the
  content tag moves, and no pre-pull covers it.
  The image is named `karakeep`, not `karakeep-time-tagger`: `k8s/manifests` keys
  `k8s_rebuilt_images` on `manifests_service`. The pod the rebuild has to roll is the tagger's,
  which `manifests_extra_rollouts` already covers.
  The main pod's `wait-for-deps` initContainer runs this image too — it is the role's only image
  with a `python3` and no s6 init — so both init containers assert uid 1000 as well.
- `files/karakeep-time-tagger.py` is vendored (not fetched at render time) because CI renders
  every template on a machine that has never deployed; `test_karakeep_time_tagger_script.py`
  pins it to the commit URL and sha256 the retired Docker role verified.
- **`karakeep-archive-pruner`** (CronJob) prunes archived bookmarks by age, and only logs a
  dry run while `karakeep_k8s_pruner_apply` is false. Rules: `files/karakeep_archive_pruner.py`.
- **The pruner's gate Job, in `karakeep-verify`.** `defaults/main.yml` names the CronJob in
  `flux_cronjob_gates`, so the renderer copies it into the Job
  `karakeep-archive-pruner-deploy-gate-<spec digest8>`, which Flux runs once per role revision
  after `karakeep` reads Ready. It replaced `k8s/cronjob-gate`'s run. With
  `karakeep_k8s_pruner_apply` true the run deletes, so the gate is a mutation and names its
  rerun-safe test,
  `ansible/roles/k8s/karakeep/tests/test_archive_pruner.py::test_a_second_run_changes_nothing`.
  It keeps the CronJob's `app: karakeep-archive-pruner` label, which karakeep's `netpol_from`
  already admits. Two things changed with the move, and the flip accepted both:
    - **A failed run now holds the revision.** Any non-zero exit fails `karakeep-verify`,
      including the pruner's deliberate abort on a bookmark that comes back without its tags.
      `k8s/cronjob-gate` only reported a non-zero exit.
    - **`Forbid` no longer keeps the 05:15 firing off a gate run.** The copy carries no
      ownerReference to the CronJob. The rules key on age, so an overlapping run acts only on
      what is already due.
- The snapshot/revert pair (`k8s/volume-snapshot`/`k8s/volume-revert`) covers `karakeep-data`
  only — reverting it alone desyncs the search index until a manual reindex.
- `karakeep-chrome` runs on a read-only root, so every path headless chromium writes needs its
  own emptyDir: `/tmp` (profile, crash dumps, the shm files `--disable-dev-shm-usage` moves out
  of `/dev/shm`) and `/var/cache/fontconfig`. The fontconfig one fails quietly — chromium still
  starts, logs `Fontconfig error: No writable cache directories`, and rescans the font tree on
  every process start. The `karakeep-chrome-write-paths-are-emptydirs` row of
  `ansible/tests/k8s/_workload_property_rows.py` is the guard; add a path to its
  `CHROME_WRITABLE_PATHS` when a chromium flag makes it write somewhere new.
