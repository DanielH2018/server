# n8n — Workflow automation

n8n with an external task-runner sidecar. See repo-root `CLAUDE.md`.

> **Migrated to k3s on 2026-08-06.** This role is the live service on daniel-box; deploy it
> with `--tags n8n` from there. This role also builds its two images in-cluster with
> BuildKit, from `templates/Dockerfile.j2`, `templates/Dockerfile-runners.j2` and
> `templates/config/n8n-task-runners.json.j2`. The builds lived in a separate `n8n-images`
> role until #2813 folded them in.
> The Docker role's compose template is gone (recover it from git history if ever needed);
> `containers/n8n/data` is still on disk from the migration.

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
  n8n-runners sub-deployment; (2) migrating state — Recreate + RWO volume-claim PVC (n8n-data)
  holding the encryption key + credentials DB. COUPLING NOTE for a future promotion: n8n
  declares TWO claims, n8n-data and n8n-files — a revert of one without the other desyncs which
  workflow-referenced /files paths actually exist against the credentials/workflow state in
  n8n-data
<!-- /generated_from -->

- **This role builds both images** through two ordered `include_role: k8s/image-builder`
  calls at the top of `tasks/main.yml`: `templates/Dockerfile.j2` (`n8n`) and
  `templates/Dockerfile-runners.j2` (`n8n-runners`). They are ordered rather than parallel
  because n8n and its task runners are version-coupled. They run before the render because
  the two image pins read the `k8s_built_image_tags` fact the builds publish; rendered without
  it, the pins fall back to `:latest` and a Recreate pod stop-starts onto the same digest.
- **Base images:** each Dockerfile is `FROM` the upstream `:stable` channel tag with a digest
  beside it. Renovate bumps the digest; the tag holds the channel. No `*_image:` var names an
  upstream image, so a bump ships only through an operator-driven `--tags n8n` deploy.
- **Host:** daniel-box (k8s) · **Port:** 5678
- **Network:** the cluster pod network. The broker binds `0.0.0.0:5679` (n8n has no
  per-interface bind option), so the `n8n-broker` NetworkPolicy in
  `templates/networkpolicy.yaml.j2` is what fences 5679 to `app: n8n-runners`. Since
  2026-09-17 the same policy fences 5678 to `app: traefik` and `app: monitor-bridge` (#1926);
  before that any pod could reach the web port and skip Authelia, CrowdSec and the rate-limit.
  The `n8n-netpol-probe` Job (`templates/netpol-probe-job.yaml.j2`, applied by
  `tasks/main.yml`) verifies both fences on every deploy. `n8n_runner_auth_token` is the second layer, not the
  only one.
- **Depends on:** traefik, authelia
- **Config in:** `defaults/main.yml` — images, sizing, claims and the auto-deploy stance. The
  `containers_list` entry in `ansible/inventory/host_vars/daniel-box.yml` selects the role and
  carries its deploy metadata only.

## Notable
- **`n8n-runners` executes arbitrary workflow code** — the resource cap on it is the main
  DoS guard. It reaches the main container's broker at `n8n:5679` over the pod network, using
  `n8n_runner_auth_token` (from secrets) behind the `n8n-broker` NetworkPolicy.
- **`/webhook/` bypasses Authelia** (public webhooks) via a dedicated higher-priority
  Traefik router. `/webhook-test/` is intentionally NOT exposed (dev-only endpoint).
- Both images are built — update via redeploy, not Watchtower.
- The runners image `COPY`s exactly one file, `n8n-task-runners.json.j2`, staged via
  `image_builder_context` — the ConfigMap mount key must match the `COPY` path exactly.
- **The one npm package the n8n image adds, `fuzzball`, is pinned by exact version** (#2213,
  2026-09-21). A renovate.json regex manager reads the pin over the npm datasource and opens a
  manual PR in its own `n8n fuzzball` group; a merged bump ships only when a `--tags n8n`
  deploy rebuilds the image. `ansible/tests/services/test_n8n_build_is_pinned.py` refuses a
  bare or ranged install and asserts the manager's matchString still finds the pin.
- **DR / encryption key:** the credential-encryption key lives in `/home/node/.n8n/config` and
  the encrypted credentials in `/home/node/.n8n/database.sqlite` — both on the `n8n-data` PVC
  (`n8n_k8s_claim`), which the Deployment mounts at `/home/node/.n8n`, so Longhorn backs them
  up together. Deliberately **NOT** also pinned in SOPS: it's redundant (key + credentials are
  co-located, so losing the claim loses both — a separate SOPS copy of the key can't decrypt
  credentials that are gone), and setting `N8N_ENCRYPTION_KEY` to anything but the on-disk key
  crashes n8n with a key-mismatch. Don't "harden" this by adding it to secrets.

## Community node packages are PVC state this repo cannot describe

`N8N_COMMUNITY_PACKAGES_ENABLED=true` in `deployment.yaml.j2` since 2026-09-10 (#1449). n8n's
own default is disabled, and the variable was unset before that, so the instance ran on the
default rather than on a decision.

The two sibling scope flags are set explicitly beside it (#1588):
`N8N_UNVERIFIED_PACKAGES_ENABLED=true` holds the behaviour 2.38.5 warns it will change in v3,
and `N8N_REINSTALL_MISSING_PACKAGES=false` holds upstream's default. The template carries the
reasoning for each, including why `N8N_COMMUNITY_PACKAGES_ALLOW_TOOL_USAGE` is not set — that
variable does not exist at 2.38.5.

**Where a package lands.** n8n npm-installs each community package under
`$N8N_USER_FOLDER/.n8n/nodes`. `N8N_USER_FOLDER` is unset across this repo, so the path is
`/home/node/.n8n/nodes` — a child of the `n8n-data` PVC (`n8n_k8s_claim`), which the Deployment
mounts at `/home/node/.n8n`. The `n8n-cache` emptyDir shadows `/home/node/.n8n/.cache` only, so
it does not cover `nodes`. Installed packages therefore live on the volume, not in the image.

**Two consequences an operator has to carry, because git cannot.**

- A package survives a pod restart, an image rebuild and a `--tags n8n` redeploy, and **nothing
  in this repo records which packages are installed**. Restoring `n8n-data` from its Longhorn
  backup restores them with it; a fresh claim starts with none, and the workflows that used them
  break at run time rather than at deploy time. Same class of state as the encryption key above.
- A package is npm-installed against the **running image's** Node runtime. A base-image Node
  major bump in this role's Dockerfiles can break a package with native dependencies while every
  manifest and template here reads unchanged.

**How to list what is installed — an operator does it, a Claude session cannot.** Both
mechanical routes are closed: `kubectl exec` is refused to the read-only ServiceAccount, so
neither `ls ~/.n8n/nodes/node_modules` nor a query against the `installed_packages` table in
`database.sqlite` is reachable, and n8n carries **its own owner login on top of Authelia**, so
`GET /rest/community-packages` answers `{"status":"error","message":"Unauthorized"}` even with a
valid two_factor Authelia session (measured 2026-09-10). code-server and FreshRSS are the same
shape — see the `TWO_FACTOR_SERVICES` comment in `scripts/diagnostics/tests/test_ui_smoke.py`.

- **The operator**, signed in to n8n as the owner, opens **Settings → Community nodes**. That
  panel lists each package with its version and is also the install and uninstall path.
- **A session** can confirm the switch but not the contents: `GET /rest/settings` needs no n8n
  login, only the Authelia cookie (`authelia_session_k8s`, minted by
  `scripts/diagnostics/ui_login.py --two-factor`), and its response carries
  `communityNodesEnabled`. Curl it with `--resolve <host>:443:<MetalLB ingress VIP>`, the same
  DNS pin `probe_lib/core.py` uses.

## Every digest bump appends a row to `base-pin-history.tsv`

The `FROM`s pin a channel tag with a digest beside it, so a bump changes 64 hex characters and
no version string. The diff cannot show which way the version moved: Renovate PR #1440
(2026-09-09) proposed moving both files from the 2.37.10 digests to the 2.37.9 digests — a
downgrade of the running n8n — and passed all nine checks. A human resolving each digest to its
version by hand is what caught it (issue #1493).

`base-pin-history.tsv` records the version behind each adopted digest, append-only, and its own
header carries the registry commands for resolving one. `scripts/tests/test_renovate_dockerfiles.py`
fails until the row is appended, and fails again if a version decreases with no `DOWNGRADE-ACK:`
note. An acknowledged decrease passes on purpose — a channel pin follows what upstream promotes,
so a withdrawn release has to be followable; what the guard forbids is a silent decrease.

## Editing
- Images: `templates/Dockerfile*.j2` + `templates/config/n8n-task-runners.json.j2`
