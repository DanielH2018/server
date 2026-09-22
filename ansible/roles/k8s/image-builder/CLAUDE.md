# k8s/image-builder — builds one image in-cluster and pushes it to the in-cluster registry

Generic, callers set `image_builder_name`/`_dockerfile`/`_context`/`_tag` rather than each
service carrying its own build block — that would copy the securityContext reasoning into
every caller, where it would drift. See repo-root `CLAUDE.md` for shared conventions.

**No standalone deploy tag.** Callers reach it via `include_role: name: k8s/image-builder`,
not `--tags image-builder` — it is not a `containers_list` entry either, so a promoted bump
to `image_builder_image` (the BuildKit tool itself) would match no play and deploy nothing
while reporting success.

## At a glance
- **Builder image:** `moby/buildkit:v0.32.2-rootless` (`image_builder_image`) — rootless
  BuildKit, not kaniko (archived upstream) or the daemonful BuildKit variant (wants a
  privileged pod). Runs as uid 1000 with no added capabilities.
- **Callers:** `ical-proxy`, `terraria`, `pi-peer-backup`, `n8n-images`, `code-server`,
  `nut`, `homelab-mcp` — each `include_role`s this with its own `image_builder_*` vars.
- **Auto-deploy: denylisted.** Renders a Job and a ConfigMap, no Deployment — nothing for
  `rollout status` to gate. Builds the images every other role above consumes, so a bad
  build here is upstream of all of them.

## Notable
- **Every build pushes TWO names for one manifest**, from one `--output`: the mutable
  `image_builder_tag` (`latest`) and an immutable `image_builder_content_tag`
  (`sha-<12 hex>`) derived from the build inputs. Both resolve to the same digest, so every
  digest comparison here reads one object either way. The content tag is what makes "is the
  running pod the image this commit describes" a comparison of NAMES rather than a registry
  digest read (`post_tasks/k8s_image_drift_gate.yml`).
  - The hash is taken over `context-configmap.yaml.j2`'s own render, `data` only — that
    mapping IS every byte the build reads, so no second list of inputs can fall out of step
    with it, and a comment edit in that template does not rebuild all nine images.
  - Computed on the CONTROLLER and before the render, so `--check` and a `--skip-tags deploy`
    run compute the same name a deploy does.
  - Published as the play-scoped `k8s_built_image_tags` (name → tag), for the roles that
    DEPLOY these images. A consumer reads `k8s_built_image_tags.get('<name>', 'latest')`, so
    a deploy that skips the building role — `--tags n8n` without `n8n-images` — falls back to
    the mutable alias instead of naming a tag nothing pushed. **No consumer reads it yet**;
    converting the nine `*_k8s_image` pins is one PR each.
  - `registry-gc.sh` prunes superseded content tags (`registry_k8s_keep_content_tags`), since
    a tag is a real manifest reference that `garbage-collect --delete-untagged` never reclaims.
- Skips the actual build when the rendered context is byte-identical to the last run's and
  the registry already serves BOTH tags — saved ~106s across the seven original callers.
  `image_builder_force=true` overrides it, for a base-image CVE bump nothing here can see.
  The content-tag clause is what keeps the tag from being decorative: `:latest` being served
  says nothing about whether the `sha-…` name exists, and an unchanged context with `:latest`
  present is exactly the state the first deploy after this landed was in.
- **A failed build forces the next one.** The gate otherwise reads every input as unchanged
  after a failure — the rendered context is still on disk and the registry still serves the
  previous tag — so the next deploy skipped the rebuild and the workload kept the old image
  behind a `failed=0` recap (#1534). The extra clause reads the previous Job's
  `.status.succeeded`. An ABSENT Job does not force a build: `ttlSecondsAfterFinished: 86400`
  collects a completed one after a day.
- **The build wait polls both terminal conditions.** `kubectl wait` takes one condition and a
  failed Job never gets `condition=complete`, so the earlier `||` fallback blocked the deploy
  for the whole `image_builder_timeout` after a build had already failed — ~30 minutes for
  code-server (#1535). `ansible/tests/deploy/test_image_builder_wait_returns_promptly.py` runs the
  wait against a stubbed `k3s` and times it.
- Reads the registry's pre- and post-build digest around every build, even a skipped one, so
  a stale pod in front of an unchanged tag still surfaces to the post-deploy drift gate
  (`ansible/tests/deploy/test_built_image_drift_gate.py`).

## Editing
- Task logic: `tasks/main.yml` · Job/ConfigMap shape: `templates/build-job.yaml.j2`,
  `templates/context-configmap.yaml.j2`.
- Not deployed standalone — deploy a caller instead, e.g.
  `uv run ansible-playbook ansible/deploy.yml --tags "ical-proxy"`.
