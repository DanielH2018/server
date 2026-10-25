# n8n — Workflow automation

n8n with an external task-runner sidecar. See repo-root `CLAUDE.md`.

> **Migrated to k3s on 2026-08-06, and handed to Flux on its Wave C flip (#5546).** Flux
> applies it through `promote`; `deploy.sh` refuses `--tags n8n`. Its two images are built
> from `templates/Dockerfile.j2`, `templates/Dockerfile-runners.j2` and
> `templates/config/n8n-task-runners.json.j2`. The Docker role's compose template is gone, and
> only git history has it; `containers/n8n/data` is on disk.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses `--tags=n8n`
  with exit 5
- **Images:** `ghcr.io/danielh2018/n8n` (`n8n_k8s_image`), `ghcr.io/danielh2018/n8n-runners`
  (`n8n_k8s_runners_image`), `alpine` (`n8n_k8s_wait_image`)
- **Route:** `n8n.<domain>` · `n8n.local.<domain>`, Authelia two_factor
- **Claims:** `n8n-data` (weekly -> B2 (default target)), `n8n-files` (weekly -> B2 (default
  target))
<!-- /generated_from -->

- **Flux applies it, not Ansible** (#5546, Wave C). Its `containers_list` entry carries
  `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it. A merged change
  reaches the cluster when `promote` publishes and verifies the next bundle. To give it back
  to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of the
  flag leaves two writers on the objects. `k8s_autodeploy: false` still stops Renovate's
  automerge of a base-digest bump.
- **Built by CI.** The promote workflow's `images` job builds both images and pushes
  `ghcr.io/danielh2018/n8n` and `ghcr.io/danielh2018/n8n-runners` under their content tags. A
  changed build input moves the `sha-…` tag, so the pod template changes and Flux rolls the
  pod. The defaults name `<k8s_registry_pull_host>`; the renderer rewrites each built ref to
  its GHCR one with `imagePullPolicy: IfNotPresent`
  (`scripts/renderer/renderer_lib/images.py:ghcr_refs`).
- **The new images are pulled before the old pod stops.** `n8n` is `Recreate`, and a base
  bump re-pulls about 465 MB from GHCR at 0.15-0.36 MB/s, so the role sets
  `flux_prepull_timeout: 3600s`. The render adds the `n8n-prepull` Kustomization, and `n8n`
  depends on it. Its one Job runs all three images the two Deployments pull: n8n, n8n-runners
  and the runners' `alpine` init container. The Job copies the pods' placement, a
  *preferred* node affinity for daniel-box, so the Job and both pods usually land there but
  are not guaranteed to. A pod the scheduler sends to daniel-server after the Job ran pays the
  cold pull inside the 600s rollout budget. Pinning both to daniel-box with a required
  affinity would close that gap, at the cost of no failover while daniel-box is down; that
  is an operator choice this role has not made. `defaults/main.yml` has the measurements, and
  `docs/flux-deploy-plane.md`, *The pre-pull Job*, has the mechanism.
- **Where the tasks went.** `tasks/main.yml` holds the two `k8s/image-builder` includes and
  the `k8s/manifests` include, and no Flux path runs any of them. They stay for the handback.
  The builds became the `images` job, and the Kustomization's `wait` replaces the rollout
  wait. The broker-isolation probe that followed them became the `n8n-verify` Job, and its
  tasks were deleted.
- **Base images:** each Dockerfile is `FROM` the upstream `:stable` channel tag with a digest
  beside it. Renovate bumps the digest, the tag holds the channel, and no `*_image:` var names
  an upstream image. A merged bump moves both content tags, and `promote` builds and rolls
  them.
- **Host:** daniel-box · **Port:** 5678 · **Depends on:** traefik, authelia
- **Network:** the cluster pod network. The broker binds `0.0.0.0:5679` (n8n has no
  per-interface bind option), so the `n8n-broker` NetworkPolicy in
  `templates/networkpolicy.yaml.j2` is what fences 5679 to `app: n8n-runners`. Since 2026-09-17
  the same policy fences 5678 to `app: traefik` and `app: monitor-bridge` (#1926); before that
  any pod could reach the web port and skip Authelia, CrowdSec and the rate-limit.
  The `n8n-verify` Job (`templates/verify/job.yaml.j2`, running `files/n8n_verify.py`)
  verifies both fences after every apply; Flux runs it in the `n8n-verify` Kustomization once
  `n8n` reads Ready. `n8n_runner_auth_token` is the second layer, not the only one.
  `job.yaml.j2` carries the Job's caller census.
- **Config in:** `defaults/main.yml` — images, sizing, claims and the auto-deploy stance. The
  `containers_list` entry in `ansible/inventory/host_vars/daniel-box.yml` selects the role, and
  carries deploy metadata only.

## Notable
- **`n8n-runners` executes arbitrary workflow code** — the resource cap on it is the main DoS
  guard. It reaches the broker at `n8n:5679` over the pod network, using
  `n8n_runner_auth_token` behind the `n8n-broker` NetworkPolicy.
- **`/webhook/` bypasses Authelia** (public webhooks) via a dedicated higher-priority Traefik
  router. `/webhook-test/` is intentionally NOT exposed (dev-only).
- Update both images by merging a change to their build inputs, not through Watchtower. The
  runners image `COPY`s exactly one file, `n8n-task-runners.json.j2`, staged via
  `image_builder_context`; the context key must match the `COPY` path exactly.
- **The one npm package both images add, `fuzzball`, is pinned by exact version** (#2213,
  #5391). One renovate.json regex manager reads both pins over the npm datasource and opens a
  manual PR in its own `n8n fuzzball` group; a merged bump moves both content tags, and
  `promote` builds and rolls them. `ansible/tests/services/test_n8n_build_is_pinned.py` refuses a bare or
  ranged install in either Dockerfile and asserts the manager's matchString finds both pins.
- **The runners image installs fuzzball outside the runner's own tree.** A `pnpm add` inside
  `/opt/runners/task-runner-javascript` prunes everything its `package.json` does not declare,
  `@n8n/di` included, so `Dockerfile-runners.j2` adds fuzzball in `/opt/runners/extra-js` and
  links it into the runner's `node_modules`. The Dockerfile's comment has the measurements.
- **DR / encryption key:** the credential-encryption key lives in `/home/node/.n8n/config` and
  the encrypted credentials in `/home/node/.n8n/database.sqlite` — both on the `n8n-data` PVC
  (`n8n_k8s_claim`), which the Deployment mounts at `/home/node/.n8n`, so Longhorn backs them
  up together. Deliberately **NOT** also pinned in SOPS: losing the claim loses both, so a
  SOPS copy of the key would have nothing to decrypt, and setting `N8N_ENCRYPTION_KEY` to
  anything but the on-disk key crashes n8n. Don't "harden" this by adding it to secrets.

## Community node packages are PVC state this repo cannot describe

`N8N_COMMUNITY_PACKAGES_ENABLED=true` in `deployment.yaml.j2` since 2026-09-10 (#1449), with
the two sibling scope flags set beside it (#1588); the template carries the reasoning for each.

n8n npm-installs each package under `/home/node/.n8n/nodes`, a child of the `n8n-data` PVC, so
a package survives a restart, a rebuild and a redeploy — and **nothing in this repo records
which packages are installed**. A fresh claim starts with none, and the workflows that used
them break at run time — the same class of state as the encryption key above.

Listing them is an operator's job, in **Settings → Community nodes**; a session cannot, because
`kubectl exec` is refused to the read-only ServiceAccount and n8n's owner login sits on top of
Authelia. `docs/n8n-community-packages.md` has the whole account.

## Every digest bump appends a row to `base-pin-history.tsv`

The `FROM`s pin a channel tag with a digest beside it, so a bump changes 64 hex characters and
no version string, and the diff cannot show which way the version moved. Renovate PR #1440
(2026-09-09) proposed the 2.37.9 digests over the running 2.37.10 — a downgrade — and passed
all nine checks; a human resolving each digest by hand caught it (#1493).

`base-pin-history.tsv` records the version behind each adopted digest, append-only; its header
carries the registry commands for resolving one.
`scripts/tests/test_renovate_dockerfiles.py` fails until the row is appended, and fails again
if a version decreases with no `DOWNGRADE-ACK:` note; what it forbids is a SILENT decrease.

## Editing
- Images: `templates/Dockerfile*.j2` + `templates/config/n8n-task-runners.json.j2`
