# n8n community node packages — where they live, and how to list them

`ansible/roles/k8s/n8n/CLAUDE.md` is the role doc, and it carries the one rule this page's
detail reduces to: an installed community package is PVC state, so restoring a fresh
`n8n-data` claim starts with none and the workflows that used them break at run time. This
page is the account behind that — which flags are set and why, where a package lands on disk,
and the two routes for reading what is installed. Kept off the role doc the inject hook loads
on every touch of the role (#2985).

`N8N_COMMUNITY_PACKAGES_ENABLED=true` in `deployment.yaml.j2` since 2026-09-10 (#1449). n8n's
own default is disabled, and the variable was unset before that, so the instance ran on the
default rather than on a decision.

The two sibling scope flags are set explicitly beside it (#1588):
`N8N_UNVERIFIED_PACKAGES_ENABLED=true` holds the behaviour 2.38.5 warns it changes in v3,
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
valid two_factor Authelia session (measured 2026-09-10). code-server and `FreshRSS` are the same
shape — see the `TWO_FACTOR_SERVICES` comment in `scripts/diagnostics/tests/test_ui_smoke.py`.

- **The operator**, signed in to n8n as the owner, opens **Settings → Community nodes**. That
  panel lists each package with its version and is also the install and uninstall path.
- **A session** can confirm the switch but not the contents: `GET /rest/settings` needs no n8n
  login, only the Authelia cookie (`authelia_session_k8s`, minted by
  `scripts/diagnostics/ui_login.py --two-factor`), and its response carries
  `communityNodesEnabled`. Curl it with `--resolve <host>:443:<MetalLB ingress VIP>`, the same
  DNS pin `probe_lib/core.py` uses.
