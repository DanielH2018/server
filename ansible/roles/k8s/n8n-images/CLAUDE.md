# n8n-images — builds n8n's two container images in-cluster

n8n has not migrated to its own k8s role. This role is the half that had to come first:
it builds the `n8n` and `n8n-runners` images into the cluster registry so a future `k8s/n8n`
role has something to run. It renders no manifest of its own.

## At a glance
- **Renders nothing** — two `include_role: k8s/image-builder` calls, ordered rather than
  parallel because n8n and its task runners are version-coupled.
- **Images:** `templates/Dockerfile.j2` (`n8n`) and `templates/Dockerfile-runners.j2`
  (`n8n-runners`), each `FROM` the upstream `:stable` channel tag with a digest beside it.
  Renovate bumps the digest; the tag holds the channel.
- **Deploy tag:** `--tags "n8n-images"`. `k8s_autodeploy: true`, but nothing can trigger the
  promotion in practice — the role declares no `*_image:` var, so an upstream bump never
  produces an image-only diff under `defaults/main.yml`. A bump ships only via an
  operator-driven rebuild: `./scripts/deploy.sh --tags n8n-images,n8n`.
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list` (a separate
  `n8n` entry follows it).

## Notable
- The runners image `COPY`s exactly one file, `n8n-task-runners.json.j2`, staged via
  `image_builder_context` — the ConfigMap mount key must match the `COPY` path exactly.
- Since `k8s_autodeploy` is a no-op here, a bad upstream image can still land on `n8n`'s next
  unrelated deploy with nothing here catching it first — see `defaults/main.yml`'s
  `k8s_autodeploy_reason` for the full argument.

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
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "n8n-images,n8n"`
