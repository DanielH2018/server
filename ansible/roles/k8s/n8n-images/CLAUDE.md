# n8n-images — builds n8n's two container images in-cluster

This role builds the `n8n` and `n8n-runners` images into the cluster registry for
`k8s/n8n`, the live workflow service, to run. It renders no manifest of its own.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "n8n-images,n8n"`
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

- **Renders nothing** — two `include_role: k8s/image-builder` calls, ordered rather than
  parallel because n8n and its task runners are version-coupled.
- **The entry is tagged `n8n` as well as `n8n-images`**, so `--tags n8n` runs this role too.
  n8n's two pins read the `k8s_built_image_tags` fact only this role publishes; deployed alone,
  n8n renders `:latest`, which changes the Deployment spec away from the content tag and
  stop-starts a Recreate pod onto the same digest.
- **Images:** `templates/Dockerfile.j2` (`n8n`) and `templates/Dockerfile-runners.j2`
  (`n8n-runners`), each `FROM` the upstream `:stable` channel tag with a digest beside it.
  Renovate bumps the digest; the tag holds the channel.
- **Auto-deploy-eligible, but nothing can trigger the promotion in practice** — the role declares no `*_image:` var, so an upstream bump never
  produces an image-only diff under `defaults/main.yml`. A bump ships only via an
  operator-driven rebuild: `./scripts/deploy.sh --tags n8n-images,n8n`. A separate `n8n`
  entry follows this one in `containers_list`.

## Notable
- The runners image `COPY`s exactly one file, `n8n-task-runners.json.j2`, staged via
  `image_builder_context` — the ConfigMap mount key must match the `COPY` path exactly.
- Since `k8s_autodeploy` is a no-op here, a bad upstream image can still land on `n8n`'s next
  unrelated deploy with nothing here catching it first — see `defaults/main.yml`'s
  `k8s_autodeploy_reason` for the full argument.
- **The one npm package the n8n image adds, `fuzzball`, is pinned by exact version** (#2213,
  2026-09-21). A renovate.json regex manager reads the pin over the npm datasource and opens a
  manual PR in its own `n8n fuzzball` group; a merged bump ships only when
  `deploy.sh --tags n8n-images,n8n` rebuilds the image.
  `ansible/tests/services/test_n8n_build_is_pinned.py` refuses a bare or ranged install and
  asserts the manager's matchString still finds the pin.

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
- Dockerfiles: `templates/Dockerfile.j2`, `templates/Dockerfile-runners.j2`
- Deploy: `./scripts/deploy.sh --tags "n8n-images,n8n"`
