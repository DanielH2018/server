# texbrain — browser-based LaTeX editor

Static SvelteKit build served by nginx. LaTeX compiles in the browser through a SwiftLaTeX
WebAssembly build of pdfTeX; git runs on isomorphic-git; files are read and written through
the File System Access API. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/danielh2018/texbrain:latest@sha256:…`, built by the fork below
- **Host:** daniel-box or daniel-server (k8s), preference only — added 2026-08-26
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`
- **State:** none. No Secret, no PVC, no backup job — the pod serves files and holds nothing

## The image comes from a fork, and why

[`DanielH2018/texbrain`](https://github.com/DanielH2018/texbrain) forks
`swimmingbrain/texbrain` and adds three files: a `Dockerfile`, `docker/nginx.conf`, and
`.github/workflows/image.yml`. It touches nothing under `src/`, so `git merge upstream/main`
stays conflict-free.

Upstream publishes no container image, no tags and no releases while committing daily.
Pinning it directly would mean a bare commit SHA that Renovate cannot offer an update for —
the failure `renovate.json` already records for the shellcheck-py pin. The fork's CI pushes
`:latest` on every merge to `main`, so the `:latest@sha256` pin here gets ordinary digest-bump
PRs.

**To take a new upstream version:** merge `upstream/main` into the fork's `main`. Its CI
rebuilds and pushes, and Renovate then opens the digest bump against
`texbrain_k8s_image`. Do not hand-edit the digest here.

## Three nginx rules are load-bearing

They live in the fork's `docker/nginx.conf`, commented there. Repeated here because a
symptom shows up in this cluster, not in that repo:

- `try_files $uri $uri.html` — `adapter-static` leaves `trailingSlash` at `never`, so every
  route is a flat `.html` file. Without the `$uri.html` arm, `/editor` 404s.
- `no-cache` on `_app/version.json` — that file backs `version.pollInterval` (60s), which is
  how an open tab notices a new deploy. Cache it and a tab after a rollout requests chunks
  that no longer exist.
- `immutable` on `_app/immutable/` — content-hashed by the build.

## Two things that look like bugs and are not

- **The pod dials nothing, but the app reaches the internet.** jsDelivr serves on-demand TeX
  packages beyond the 86 MB subset baked into the image, and a CORS proxy carries git
  push/pull. Both are the *browser's* fetches, from the reader's machine. That is why
  texbrain is in `BORN_FENCED_ROLES` in `ansible/tests/test_netpol_baseline_labels.py`.
- **An expired Authelia session can leave the app serving HTML as JavaScript.** The service
  worker's `handleImmutable` caches any 200 under a content-hashed name and treats it as
  permanent, so a chunk fetch that follows the redirect to the login page caches the portal.
  The TeX package chain is immune — it runs every payload through `looksValid()` and falls
  through to the CDN — but the app shell has no such guard. It clears on the next deploy,
  since the app cache is keyed by build version, and on a hard reload.

## Editing
- Manifests: `templates/` · Image pin and resources: `defaults/main.yml`
- Deploy: `./scripts/deploy.sh --tags "texbrain"`
- Verify: `uv run python scripts/diagnostics/probe.py health texbrain`, then open the page and
  compile a document — a 1/1 pod says nothing about whether the WebAssembly engine ran.
