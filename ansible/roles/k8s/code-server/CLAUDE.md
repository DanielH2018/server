# code-server — Browser-based VS Code

Moved to k3s on 2026-08-10 (slice-7 Phase C). This role owns both the workload and the image
it is built from (`templates/Dockerfile.j2` + `files/extensions.sh`); the inventory entry is in
`daniel-box.yml`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=code-server` with exit 5
- **Image:** `ghcr.io/danielh2018/code-server` (`code_server_k8s_image`)
- **Route:** `code-server.<domain>` · `code-server.local.<domain>`, Authelia two_factor
- **Claims:** `code-server-config` (no backup (listed in k3s_longhorn_nobackup_volumes)),
  `code-server-workspace` (weekly -> B2 (default target))
<!-- /generated_from -->

- **Image:** built by the promote workflow's `images` job from `templates/Dockerfile.j2`,
  pushed to `ghcr.io/danielh2018/code-server:<content tag>` (D5.4). In-cluster by
  k8s/image-builder before D5.16.
- **Host:** daniel-box (k3s), pinned by a required node affinity · **Port:** 8443
- **Flux applies it, not Ansible** (D5.16, Wave C). Its `containers_list` entry carries
  `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a merged
  change reaches the cluster when `promote` publishes and verifies the next bundle. To give it
  back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of
  the flag leaves two writers on the objects. `k8s_autodeploy: false` still stops Renovate's
  automerge of a base-image or build-pin bump.
- **Both claims are snapshotted before each revision applies.** The role declares
  `k8s_autodeploy_snapshot_pvcs`, so the render adds the `code-server-snapshot` Kustomization,
  and `code-server` depends on it. Its Job, `snapshot-code-server-<digest8>`, snapshots
  `code-server-config` and `code-server-workspace` and must pass before `code-server` applies
  (`docs/flux-deploy-plane.md`, *The snapshot Job*). It is the first live snapshot role, so it
  is also the first tag the stateful hold covers: a failed revision that changes code-server
  holds `DEPLOY_HOLD` without a reset, and the way out is the operator's revert runbook in
  `docs/volume-snapshot-drills.md`.
- **The password Secret substitutes from `homelab-cluster-vars`.** `code_server_password` and
  `code_server_sudo_password` are `${<key>}` placeholders in the published render. Flux
  substitutes the values from the `homelab-cluster-vars` Secret, which it decrypts from the
  SOPS cluster file in `ansible/vars/`. The Kustomization depends on `cluster-vars`, so a
  rotated value is live before code-server reconciles. A rotation reaches the cluster only
  through `cluster_vars.py write`. The renderer stamps a restart checksum on the Deployment, so
  a changed value rolls the pod.
- **An image change can be a slow pull, and the pre-pull keeps it off the outage.** The pod
  pulls from GHCR, which served both nodes at 0.2-0.5 MB/s on 2026-10-17 and at 44-95 MB/s on
  2026-10-24 (#5654). A claude-code CLI bump invalidates about 377 MB of layers, and a
  base-image bump about 851 MB, so in a slow week a pull takes roughly 20 to 50 minutes. The Deployment is `Recreate`. The `code-server-prepull`
  Kustomization (below) pulls the image onto the node while the old pod still serves, so the
  Deployment keeps the default rollout budget and the editor is down only for the container
  start.
- **Where the tasks went.** `tasks/main.yml` holds two includes, `k8s/image-builder` and
  `k8s/manifests`, and no Flux path runs either. They stay for the handback. The defaults name
  `<k8s_registry_pull_host>/code-server`; the renderer rewrites both refs to the GHCR one with
  `imagePullPolicy: IfNotPresent` (`scripts/renderer/renderer_lib/images.py:ghcr_refs`). The
  snapshot Job replaces k8s/volume-snapshot's pre-apply snapshot, and the Kustomization's
  `wait` replaces the rollout wait.
- **Ported WITHOUT docker plumbing** (operator decision 2026-08-10): no DOCKER_HOST, so the
  in-IDE docker CLI and devcontainers are gone; docker-proxy-codeserver and the `codeserver`
  net dissolved with the Docker copy (`has_code_server: false` in daniel-server host_vars).

## Notable
- Extensions are downloaded at build time (Open VSX + MS Marketplace) into `/opt/vsix`;
  `extensions.sh` installs them into /config on container start. **Every build input is
  pinned** (2026-09-21, #2149): the base image by digest, each extension by version + sha256
  in `code_server_k8s_extensions` (`defaults/main.yml`, with the bump recipe), Node and Rust by
  checksummed official tarballs, and the pip and npm packages by exact version. Renovate has
  no datasource for a VS Code extension, so those bump by hand — the base-image PR is the
  natural moment. The exception is `Anthropic.claude-code`: it moves with every Renovate
  bump of the CLI, in the same PR, and the `code-server build pins` group name says so
  (#4584). `ansible/tests/services/test_code_server_build_is_pinned.py` refuses a
  floating input.
- **Rust is one pinned stable toolchain with no rustup** (2026-10-12). The image installs
  rustc, cargo, clippy, rustfmt and rust-src from the standalone tarballs into `/usr/local`,
  plus `gcc` and `libc6-dev` for the linker. A project's `rust-toolchain.toml`, nightly and
  `rustup target add` do nothing here. The operator chose this over a pinned `rustup-init` for
  having no runtime-writable toolchain state; the Dockerfile's step 3 carries the decision. A
  Rust bump arrives in the `code-server build pins` Renovate group and needs
  `asset_pins.py --refresh rust-lang/rust` to finish both digests.
  - **Cargo writes only to the config volume.** `CARGO_HOME=/config/.cargo` and
    `CARGO_TARGET_DIR=/config/.cargo-target`, so a project's build output never lands in
    `/config/workspace`. That path is on the 1 Gi backed-up claim, and one `target/` reaches
    several GiB. Every project shares the one target directory, so `cargo run` works as usual
    but the binaries are under `/config/.cargo-target/debug/`, not `./target/`.
    `ansible/tests/services/test_code_server_build_is_pinned.py::test_cargo_writes_stay_off_the_backed_up_claim`
    refuses a cargo path under a backed-up mount.
  - The editor side is rust-analyzer, Even Better TOML and CodeLLDB. CodeLLDB is pinned to its
    GitHub release, not Open VSX; its defaults entry says why.
- **This image cannot compile LaTeX.** TeX Live, `latexmk`, LaTeX Workshop and the PDF viewer
  extension were removed on 2026-08-27 — 56 packages and ~309 MiB of the ~858 MiB installed
  here, with no caller outside the TeX set. `roles/k8s/texbrain` owns compiling, in the
  browser via a WebAssembly pdfTeX. There is no local fallback: texbrain needs the pod, a
  browser, and a jsDelivr mirror for any package outside its bundled TeX Live subset.
- **`extensions.sh` installs but does not prune**, so removing an extension from the image
  leaves it live on the config volume. Retiring one means adding its id to `RETIRED_EXTS` in
  that script, not just deleting it from the Dockerfile.
- **Under Flux, the new image is pulled before the old pod stops** (#5525). The role sets
  `flux_prepull_timeout: 4500s`, so the render adds the `code-server-prepull` Kustomization,
  and `code-server` depends on it. Its Job runs the Deployment's GHCR ref under the
  Deployment's own node affinity. The affinity is required (operator ruling 2026-10-18,
  #5547), so the Job always lands on daniel-box, the node the pod will run on, and completes
  once the kubelet holds the image. A cordoned daniel-box leaves the pod unschedulable
  instead of moving it to daniel-server. The
  `Recreate` rollout then pays only the container start, not the 20-50 minute GHCR pull.
  `docs/flux-deploy-plane.md`, *The pre-pull Job*, has the mechanism. An Ansible deploy
  ignores the key.
- **Two claims, and only the small one is backed up** (2026-08-16). `code-server-config`
  (10 Gi) holds derived state and is in this role's `containers_list` entry's
  `no_backup_claims`, so in `k3s_longhorn_nobackup_volumes`;
  `code-server-workspace` (5 Gi) holds `workspace`, `.ssh`, `.config` and the git identity,
  mounted by subPath, and is what the weekly B2 tier backs up. A first-boot initContainer
  copies them across and is a no-op afterwards. Nothing was deleted — the config volume still
  persists everything across restarts, it just is not backed up.
  Two consequences worth knowing before you touch this:
  - **The git identity lives at `/config/.config/git/config`**, not `~/.gitconfig`, which the
    migration renames to `.gitconfig.pre-split`. Mounting `~/.gitconfig` by subPath would
    break `git config --global`: it writes a `.lock` and `rename()`s it over the target, and
    a rename onto a bind-mounted file fails EBUSY.
  - **Anything new worth keeping must go under one of those four paths.** Dropping a file
    elsewhere in `/config` leaves it live but unbacked, which looks identical until a restore.
- `/config/.local/share/code-server/extensions` held 5.8 G on 2026-08-16, and the server
  never reads it — it runs with `--extensions-dir /config/extensions` (637 M). Cause found and
  fixed the same day: `extensions.sh` invoked the CLI without `--extensions-dir` from its first
  commit, so every install landed in code-server's default directory instead, and each image
  rebuild added another version of all seven without removing the last. **The fix stops it
  growing; it does not remove what accumulated.** Deleting the tree is a one-off by hand.
  `du` accounted for ~6.7 G of the 10 Gi volume on 2026-08-16, so the filesystem was around
  two-thirds full. Since 2026-10-12 the volume also holds the Rust build output and the crate
  cache, so delete the tree before Rust builds fill the remaining third. (Longhorn's `actualSize` reads ~96% because it counts the snapshot chain, not
  filesystem usage.)
- Because the image is built, there is no registry tag for Renovate to track
  (`code_server_k8s_image` is in the REGISTRY_BUILT_IMAGES carve-out). A merged change to a
  build input moves the content tag, the `images` job pushes it, and `promote` rolls the pod.
  A package refresh that no pin moves is a hand PR that bumps a `rebuild_stamp` data key in
  the context (`ansible/roles/k8s/image-builder/CLAUDE.md`).

## Editing
- Image: `templates/Dockerfile.j2`, `files/extensions.sh` · Workload: `ansible/roles/k8s/code-server/`
