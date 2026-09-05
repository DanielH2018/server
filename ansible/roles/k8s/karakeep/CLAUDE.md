# karakeep — bookmark archiving, with a search index and an AI tagger

Karakeep (the app), a `karakeep-chrome` headless-shell sidecar for page snapshots, a
Meilisearch Deployment for search, and a `time-tagger` sidecar that calls the app's API on a
loop to auto-tag bookmarks.

## At a glance
- **Deploy tag:** `--tags "karakeep"`. Route: `karakeep.<domain>`, Authelia — except `/api/v1`,
  deliberately public (Bearer-token auth; the browser extension and mobile app can't pass 2FA).
- **Persists:** `karakeep-data` (`longhorn`, backed up, ~487M) — bookmark library, page
  snapshots, `db.db`. `karakeep-meili` (`longhorn-nobackup`, ~286M) — the search index,
  deliberately unseeded and unbacked-up: it's rebuildable from `db.db` by reindexing.
- **Secrets (SOPS keys, not values):** `karakeep_meili_master_key`, `karakeep_gemini_api_key`
  (as `OPENAI_API_KEY`), `karakeep_python_api_key` (the tagger's).
- **`k8s_autodeploy: false`** for three independent reasons: meilisearch migrates its index
  in place non-atomically on a bump; the `time-tagger` sub-deployment renders no
  readinessProbe, so `rollout status` proves nothing for it; and the config PVC is `Recreate` +
  RWO. Any one alone would justify the denylist.

## Notable
- `manifests_extra_rollouts` rolls `karakeep-meilisearch` and `karakeep-time-tagger` on every
  manifest change, not just a Secret change — `MEILI_MASTER_KEY` and
  `KARAKEEP_PYTHON_API_KEY` are env vars, injected once at container start, so a key rotation
  that doesn't also roll these two sidecars leaves them on the old value while `karakeep`
  itself gets the new one.
- `files/karakeep-time-tagger.py` is vendored (not fetched at render time) because CI renders
  every template on a machine that has never deployed; `test_karakeep_time_tagger_script.py`
  pins it to the Docker role's checksum.
- The snapshot/revert pair (`k8s/volume-snapshot`/`k8s/volume-revert`) covers `karakeep-data`
  only — reverting it alone desyncs the search index until a manual reindex.
- `karakeep-chrome` runs on a read-only root, so every path headless chromium writes needs its
  own emptyDir: `/tmp` (profile, crash dumps, the shm files `--disable-dev-shm-usage` moves out
  of `/dev/shm`) and `/var/cache/fontconfig`. The fontconfig one fails quietly — chromium still
  starts, logs `Fontconfig error: No writable cache directories`, and rescans the font tree on
  every process start. `ansible/tests/services/test_karakeep_chrome_writable_paths.py` is the
  guard; add a path there when a chromium flag makes it write somewhere new.
