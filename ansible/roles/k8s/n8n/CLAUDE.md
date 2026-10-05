# n8n — Workflow automation

n8n with an external task-runner sidecar. See repo-root `CLAUDE.md`.

> **Migrated to k3s on 2026-08-06.** This role is the live service on daniel-box; deploy it
> with `--tags n8n` from there. It also builds its two images in-cluster with BuildKit, from
> `templates/Dockerfile.j2`, `templates/Dockerfile-runners.j2` and
> `templates/config/n8n-task-runners.json.j2` — a separate `n8n-images` role until #2813
> folded them in. The Docker role's compose template is gone, and only git history has it;
> `containers/n8n/data` is on disk.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "n8n"`
- **Images:** `<k8s_registry_pull_host>/n8n` (`n8n_k8s_image`),
  `<k8s_registry_pull_host>/n8n-runners` (`n8n_k8s_runners_image`), `alpine`
  (`n8n_k8s_wait_image`)
- **Route:** `n8n.<domain>` · `n8n.local.<domain>`, Authelia two_factor
- **Claims:** `n8n-data` (weekly -> B2 (default target)), `n8n-files` (weekly -> B2 (default
  target))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — two reasons: (1) probe-less
  n8n-runners sub-deployment; (2) migrating state — Recreate + RWO PVC (n8n-data) holding the
  encryption key + credentials DB. COUPLING NOTE for a future promotion: n8n declares TWO
  claims, n8n-data and n8n-files — a revert of one without the other desyncs which
  workflow-referenced /files paths actually exist against the credentials/workflow state in
  n8n-data
<!-- /generated_from -->

- **This role builds both images** through two ordered `include_role: k8s/image-builder` calls
  at the top of `tasks/main.yml`. Ordered rather than parallel, because n8n and its task
  runners are version-coupled; before the render, because the two image pins read the
  `k8s_built_image_tags` fact the builds publish. Rendered without that fact, the pins fall
  back to `:latest` and a Recreate pod stop-starts onto the same digest.
- **Base images:** each Dockerfile is `FROM` the upstream `:stable` channel tag with a digest
  beside it. Renovate bumps the digest, the tag holds the channel, and no `*_image:` var names
  an upstream image — so a bump ships only through a `--tags n8n` deploy.
- **Host:** daniel-box · **Port:** 5678 · **Depends on:** traefik, authelia
- **Network:** the cluster pod network. The broker binds `0.0.0.0:5679` (n8n has no
  per-interface bind option), so the `n8n-broker` NetworkPolicy in
  `templates/networkpolicy.yaml.j2` is what fences 5679 to `app: n8n-runners`. Since 2026-09-17
  the same policy fences 5678 to `app: traefik` and `app: monitor-bridge` (#1926); before that
  any pod could reach the web port and skip Authelia, CrowdSec and the rate-limit.
  The `n8n-netpol-probe` Job (`templates/netpol-probe-job.yaml.j2`, applied by
  `tasks/main.yml`) verifies both fences on every deploy — `n8n_runner_auth_token` is the
  second layer, not the only one.
- **Config in:** `defaults/main.yml` — images, sizing, claims and the auto-deploy stance. The
  `containers_list` entry in `ansible/inventory/host_vars/daniel-box.yml` selects the role, and
  carries deploy metadata only.

## Notable
- **`n8n-runners` executes arbitrary workflow code** — the resource cap on it is the main DoS
  guard. It reaches the broker at `n8n:5679` over the pod network, using
  `n8n_runner_auth_token` behind the `n8n-broker` NetworkPolicy.
- **`/webhook/` bypasses Authelia** (public webhooks) via a dedicated higher-priority Traefik
  router. `/webhook-test/` is intentionally NOT exposed (dev-only).
- Update both images by redeploying, not through Watchtower. The runners image `COPY`s
  exactly one file, `n8n-task-runners.json.j2`, staged via `image_builder_context`; the
  ConfigMap mount key must match the `COPY` path exactly.
- **The one npm package the n8n image adds, `fuzzball`, is pinned by exact version** (#2213).
  A renovate.json regex manager reads the pin over the npm datasource and opens a manual PR in
  its own `n8n fuzzball` group; a merged bump ships only when a `--tags n8n` deploy rebuilds
  the image. `ansible/tests/services/test_n8n_build_is_pinned.py` refuses a bare or ranged
  install and asserts the manager's matchString still finds the pin.
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
