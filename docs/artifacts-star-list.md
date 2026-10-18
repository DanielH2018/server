# artifacts star list

The artifacts browser puts a star on each card. A starred artifact is never pruned, and the
"starred only" checkbox filters the list to the starred ones. This page records how one click
in the browser reaches the prune on every host. The operating rules stay in
`ansible/roles/k8s/artifacts/CLAUDE.md`.

## One file holds every star

The server writes the star list to `artifacts_stars_name` in `artifacts_stars_dir`
(`/var/lib/artifact-stars/stars.txt` on daniel-box). The file holds one `<host>/<relpath>` line per starred artifact. `<host>` is
the `/srv/artifacts/<host>` segment of the artifact's URL, and it equals the
`CLAUDE_ARTIFACTS_HOST` label that each host's prune runs under. The prune is the dotfiles'
`prune-artifacts.sh`, which runs at session start and deletes only from that host's own tree.
It skips every artifact whose line carries its own label.

- **The list is the pod's only writable mount.** The artifact trees stay read-only. The mount
  is the directory, not the file. The server replaces the list by renaming a temp file over
  it, and a rename onto a single-file bind mount fails with EBUSY. The rename also means a
  prune never reads a half-written list, which would drop stars for that sweep.
- **The list lives outside `/home/ubuntu`.** The agent user's prune reads the same file as
  `claude`, and `claude` cannot traverse `/home/ubuntu` (`0750`). `k8s_host_files` creates the
  directory `0755`, and the server writes the file `0644`. It holds file names, not secrets.
- **The dotfiles name the same path.** `settings.base.json` there sets daniel-box's
  `CLAUDE_ARTIFACT_STARS_FILE` to it, so moving the file means changing both repos.

## A peer learns its stars from the sync

daniel-server prunes its own tree, so it needs its own copy of the list. The sync cron on
daniel-box, which `roles/setup/claude_code` installs, pushes the list from
`claude_code_artifacts_stars_file` to each peer's `stars_path` after it pulls that peer's
artifacts.
The push uses the same ssh transport as the pull, and a failed push counts toward the same
failure streak. A missing list means nothing was ever starred, and the cron pushes nothing.

A star set while the peer is unreachable reaches it on the next successful run. Until then, a
session that starts on that peer can delete an artifact that was already more than 30 days
old when it was starred. The window is at most one sync interval while the peer is up.

## The loopback server cannot set a star on a k3s node

Every host also runs the dotfiles' loopback artifact server, `serve-artifacts.sh`. On a k3s
node the links point at this service instead, and the loopback server is only a fallback.
There it shows stars but cannot set them, because the next sync would overwrite whatever it
wrote on daniel-server. A workstation has no sync, so its loopback server owns its own list
and sets stars itself.

## Files under `pinned/` are a separate mechanism

The prune exempts `pinned/` by path, which predates the star. The browser does not show those
files as starred. Removing the star from one could not let the prune delete it, so the two stay
apart.
