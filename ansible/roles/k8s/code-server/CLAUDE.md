# code-server — Browser-based VS Code

Moved to k3s on 2026-08-10 (slice-7 Phase C). This role owns both the workload and the image
it is built from (`templates/Dockerfile.j2` + `files/extensions.sh`); the inventory entry is in
`daniel-box.yml`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "code-server"`
- **Image:** `<k8s_registry_pull_host>/code-server` (`code_server_k8s_image`)
- **Route:** `code-server.<domain>` · `code-server.local.<domain>`, Authelia two_factor
- **Claims:** `code-server-config` (no backup (listed in k3s_longhorn_nobackup_volumes)),
  `code-server-workspace` (weekly -> B2 (default target))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — in-cluster-built registry image with
  no upstream version scheme — Renovate can never generate an update PR for it, so there is no
  bump event to auto-deploy regardless of the migrating-state PVC shape below
<!-- /generated_from -->

- **Image:** built in-cluster by k8s/image-builder from `templates/Dockerfile.j2`
- **Host:** daniel-box (k3s) · **Port:** 8443
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
- Because the image is built, bump it by redeploying the k8s role — there is no registry tag
  for Renovate to track (`code_server_k8s_image` is in the REGISTRY_BUILT_IMAGES carve-out).

## Editing
- Image: `templates/Dockerfile.j2`, `files/extensions.sh` · Workload: `ansible/roles/k8s/code-server/`
