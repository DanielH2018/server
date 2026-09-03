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
- Skips the actual build when the rendered context is byte-identical to the last run's and
  the registry already serves the tag — saved ~106s across the seven original callers, most
  of them byte-identical five times out of seven. `image_builder_force=true` overrides it,
  for a base-image CVE bump nothing in this role's own gate can see.
- Reads the registry's pre- and post-build digest around every build, even a skipped one —
  `image_builder_building` gates the build itself, but the digest read after it deliberately
  does not, which is what lets a stale pod in front of an unchanged tag still surface to the
  post-deploy drift gate (`ansible/tests/deploy/test_built_image_drift_gate.py`).

## Editing
- Task logic: `tasks/main.yml` · Job/ConfigMap shape: `templates/build-job.yaml.j2`,
  `templates/context-configmap.yaml.j2`.
- Not deployed standalone — deploy a caller instead, e.g.
  `uv run ansible-playbook ansible/deploy.yml --tags "ical-proxy"`.
