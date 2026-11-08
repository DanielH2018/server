# k8s/image-builder — builds one image in-cluster and pushes it to the in-cluster registry

Generic, callers set `image_builder_name`/`_dockerfile`/`_context`/`_tag` rather than each
service carrying its own build block — that would copy the securityContext reasoning into
every caller, where it would drift. See repo-root `CLAUDE.md` for shared conventions.

**No standalone deploy tag.** `--tags image-builder` selects nothing: it is not a
`containers_list` entry, and no render applies its Job.

## At a glance
- **Builder image:** `moby/buildkit` (`image_builder_k8s_image`) — rootless
  BuildKit, not kaniko (archived upstream) or the daemonful BuildKit variant (wants a
  privileged pod). Runs as uid 1000 with no added capabilities.
- **Callers:** `ical-proxy`, `terraria`, `pi-peer-backup`, `n8n`, `code-server`,
  `nut`, `homelab-mcp`, `valheim`, `karakeep` — each lists its builds'
  `image_builder_*` vars in its own `image_builder_builds` default.
- **Renovate does not automerge its bumps** (`k8s_autodeploy: false`). It renders a Job and a
  ConfigMap, no Deployment, and it builds the images every other role above consumes, so a bad
  build here is upstream of all of them.

## Notable
- **Every build has TWO names for one manifest:** the mutable `image_builder_tag` (`latest`) and
  an immutable `image_builder_content_tag` (`sha-<12 hex>`) derived from the build inputs.
  - The hash is taken over `context-configmap.yaml.j2`'s own render, `data` only. That mapping
    is every byte the build reads, so no second list of inputs can fall out of step with it,
    and a comment edit in that template does not rebuild the images.
  - The renderer computes `k8s_built_image_tags` (name → tag) for the roles that deploy these
    images. **All nine `*_k8s_image` pins read it**
    (`ansible/tests/k8s/test_built_images_name_the_content_tag.py` requires it).
  - `scripts/lib/render_guard.py:BUILT_IMAGE_TAG_STUBS` seeds one for every rendered-manifest
    guard, which would otherwise check `:latest` while production renders the hash.

## Flux-owned roles (target slice D5.4)
- **The renderer writes every context this role builds.** `homelab-render cluster` writes
  each of the ten contexts to `DIR/images/<name>/` and each content tag to
  `DIR/images/tags.json` (`scripts/renderer/renderer_lib/images.py`). The tag is
  `scripts/renderer/renderer_lib/derived.py:content_tag` over the same `data`, which equals
  this role's own expression.
- **For a flux-owned role, the content tag comes from `DIR/images/tags.json`.** The
  consumer's ref renders from the same computation, and the render refuses a ref that
  differs. `scripts/lib/render_guard.py:BUILT_IMAGE_TAG_STUBS` stays a stub for guards.
- **What replaces this role for a flux-owned role** is the promote workflow's `images` job
  (D5.4a2), which `publish` needs. It recomputes each context's content tag from the files
  under `DIR/images/<name>/`, refuses one that differs from `tags.json`, and pushes
  `ghcr.io/danielh2018/<name>:<content tag>` only when GHCR does not serve that tag
  (`scripts/deploy_tools/ci_images.py`). A served tag is never rebuilt, so the tag stays
  immutable. Every consumer pulls its image from GHCR.
- **A package refresh that no pin moves is a hand PR.** The PR bumps a `rebuild_stamp` data
  key in the context, which moves the content tag. No context carries that key yet.
  The plan is `docs/target-architecture/deploy/d5-bulk-move.md`, *Images built by CI (D5.4)*.

## Editing
- Job shape: `templates/build-job.yaml.j2`. The renderer reads
  `templates/context-configmap.yaml.j2` for each content tag.
- Not deployed standalone, and no caller reaches it: every caller is `owner: flux`. promote's
  `images` job builds their images, and `deploy.sh` refuses their tags with exit 5.
