# n8n — Workflow automation

n8n with an external task-runner sidecar. See repo-root `CLAUDE.md`.

> **Migrated to k3s on 2026-08-06.** This role is the live service on daniel-box; deploy it
> with `--tags n8n` from there. The two images are built in-cluster with BuildKit by the
> sibling `k8s/n8n-images` role, from `roles/k8s/n8n-images/templates/Dockerfile.j2`,
> `Dockerfile-runners.j2` and `templates/config/n8n-task-runners.json.j2` — edit those there.
> The Docker role's compose template is gone (recover it from git history if ever needed);
> `containers/n8n/data` is still on disk from the migration.

## At a glance
- **Images:** built by `k8s/n8n-images` from its `templates/Dockerfile.j2` (`n8n`) +
  `Dockerfile-runners.j2` (`n8n-runners`)
- **Host:** daniel-box (k8s) · **Port:** 5678 · **URL:** `n8n.<domain>` (Authelia: yes)
- **Network:** the cluster pod network. The broker binds `0.0.0.0:5679` (n8n has no
  per-interface bind option), so the `n8n-broker` NetworkPolicy in
  `templates/networkpolicy.yaml.j2` is what fences 5679 to `app: n8n-runners`. The
  `n8n-netpol-probe` Job (`templates/netpol-probe-job.yaml.j2`, applied by `tasks/main.yml`)
  verifies that fence on every deploy. `n8n_runner_auth_token` is the second layer, not the
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
  major bump in `k8s/n8n-images` can break a package with native dependencies while every
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

## Editing
- Images: `templates/Dockerfile*.j2` + `templates/n8n-task-runners.json.j2` (built/copied by
  the `n8n-images` k8s role)
- Deploy (from daniel-box): `uv run ansible-playbook ansible/deploy.yml --tags "n8n"`
