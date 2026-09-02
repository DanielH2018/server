# karakeep — bookmark archive (remote node)

## At a glance
- **Images:** karakeep + `zenika/alpine-chrome:124` + `getmeili/meilisearch:v1.53.1` + an
  `astral-sh/uv` time-tagger (all pinned; every digest verified 2026-08-30 as a multi-arch
  index carrying arm64)
- **Host:** `daniel-cloud` (Oracle A1, arm64) — the **permanent primary**, not a replica
- **Route:** `karakeep.<domain>` · **Port:** 3000 · **Networks:** `proxy`, `internal` · **Depends on:** traefik
- **Config:** `files/karakeep-time-tagger.py` → bind-mounted into the tagger
- **State:** `containers/karakeep/data/` — bookmark library, archived snapshots, `db.db`

## Notable
- **`./data` becomes the only copy once the cluster role is retired.** Arm the nightly pull
  to the cluster before that happens — Oracle Always Free has no automated backups.
- **The Meili index is deliberately NOT backed up.** It is a named volume, outside the pull,
  and is rebuildable from karakeep's own DB. Reverting `./data` alone desyncs the pair, so
  the restore runbook is "restore data, then Reindex All Bookmarks" — never "restore both".
- **The `/api/` bypass router's trailing slash is load-bearing.** It anchors the prefix to
  the path *segment*; a bare ``PathPrefix(`/api`)`` would also match siblings like
  `/api-docs` and silently extend the no-auth bypass to them.
- **Two prerequisites live in the node's Traefik role, which does not exist yet:** the
  `rate-limit@file` middleware (referenced by the shared macro, so every service needs it)
  and `csp-karakeep@file`. A router naming a middleware Traefik cannot resolve fails, so
  both must ship with that role before this one deploys.
- The traefik `labels()` macro is called directly rather than through
  `expose.yml.j2`'s `web_ui_labels`, which accepts no `extra_middlewares`. The
  whitespace-stripping dash on the preceding Jinja comment is required: the macro body
  carries its own six-space indent, and without the strip every label renders at twelve and
  the YAML breaks. (It did, on first render.)
- **Two deliberate divergences from the retired Docker role**, both dropping something that
  role needed: the uv cache is a named volume rather than a uid-1000 bind mount, so root no
  longer needs `DAC_OVERRIDE`; and the working directory is a tmpfs, so the tagger's
  unretained loguru rotation dies with the container instead of filling the writable layer.
  The cluster reached the same two answers with an emptyDir.
- `k8s_autodeploy: false` on the cluster role, for reasons that carry over — a non-atomic
  Meili index migration and a probe-less tagger. Deploy this one by hand and watch it.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Tagger script: `files/karakeep-time-tagger.py` (bind-mounted, so `tasks/main.yml` passes
  `common_config_changed` — otherwise the container keeps running the old copy)
- Keep the tagger's pinned PyPI versions identical to the cluster role's command.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "karakeep" -e target=daniel-cloud`
