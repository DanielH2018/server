# artifacts — the Claude Code artifact browser, and the cross-host sync behind it

Serves the HTML/Markdown artifacts Claude Code sessions write to `~/.claude/artifacts/`, so a
report generated in a terminal session is readable in a browser behind Authelia. Written
2026-08-24: this role had no `CLAUDE.md` at all, and it was the only role under `roles/k8s/` in
that state — which mattered because it installed a **cross-host cron** that nothing else in the
tree documented. Since H2.7 `roles/setup/claude_code` installs that cron, and this file still
documents how it behaves.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=artifacts` with exit 5
- **Image:** `python` (`artifacts_k8s_image`)
- **Route:** `artifacts.<domain>` · `artifacts.local.<domain>`, Authelia one_factor
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible** (D5.19, the first Wave D PR). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. To
  give it back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain
  revert of the flag leaves two writers on the objects. The Deployment is `RollingUpdate`
  with one replica, so a roll keeps one pod serving. The host directories it mounts are the
  setup plane's (below), so Flux creates none of them.
- **Where the task went.** `tasks/main.yml` holds one task, the include of `k8s/manifests`,
  and no Flux path runs it. It stays for the handback. The Kustomization's `wait` replaces the
  rollout wait, and the restart checksum the renderer stamps replaces the shared restart, so a
  ConfigMap edit still rolls the pod.

- **Host:** pinned to `daniel-box` (`artifacts_k8s_node`), because it bind-mounts that host's own
  artifact tree.
- **Serves:** a generated index plus the files under `artifacts_host_dir`
  (`~/.claude/artifacts`, daniel-box's own) and `artifacts_peer_dir`
  (`~/.claude/artifacts-peer`, everything pulled from peers). Stdlib only — no build, no
  extra deps.
- **This role's deploy creates none of the host directories it mounts.**
  `ansible/roles/setup/k8s_host_files/tasks/artifacts.yml` creates `artifacts_host_dir`, one
  peer directory per `artifacts_peer_sources` entry and `artifacts_stars_dir` through the
  setup plane, because a Flux-owned role runs no task (D5). Each is a `type: Directory`
  hostPath, so on a fresh host `initial_setup.yml --tags k8s_host_files` runs before the
  first `artifacts` deploy. Its copies of those four keys, in the block's `vars:`, must equal
  this role's, which `ansible/tests/setup/test_k8s_host_files_pairs.py` holds.
- **The agent user's tree is a third source.** `artifacts_agent_dir`
  (`/var/lib/claude/.claude/artifacts`, set only while `claude_code_agent_user_enabled` is true)
  mounts read-only at `/srv/artifacts/daniel-box-claude`, so the index lists it as the host
  `daniel-box-claude` beside the operator's `daniel-box`. An empty value removes the volume and
  the mount. The hostPath is `type: Directory`, never `DirectoryOrCreate`: a missing directory
  holds the new pod in ContainerCreating while the old one keeps serving, where kubelet would
  create it root-owned in the agent's home. `roles/setup/claude_code` creates the directory
  (`2750 claude:ubuntu`, default ACL `g:ubuntu:rX`). The pod runs as `puid:pgid`, not root, so
  it reads that tree through the `ubuntu` group grant.

## The peer sync runs from daniel-box, and the direction is not arbitrary
`ansible/roles/setup/claude_code/tasks/node_crons.yml` installs `/usr/local/bin/sync-artifacts.sh`
and a cron (`claude_code_artifacts_sync_minute`, every 5 minutes) that rsyncs each entry in
`claude_code_artifacts_peer_sources` onto daniel-box, then pushes the star list to the peer's
`stars_path`. This role keeps the twin keys the pod reads, `artifacts_peer_sources` and
`artifacts_peer_dir`, and
`ansible/tests/setup/test_claude_code_artifacts_node_crons.py` holds each pair equal. A
change to the peer list therefore edits both roles and needs both applies: `deploy.sh --tags
artifacts` for the mounts, and `initial_setup.yml --tags claude_code` for the sync.

**daniel-box opens every connection.** The reason is written at the task and is worth repeating
because it looks backwards at first glance: the name `daniel-box` does not resolve from
daniel-server (measured), and a peer-side job would need an ssh key inside the cluster.
It is a host cron rather than a sidecar for the same reason — **the ssh credential stays on the
host and never enters a pod.**

Consequences worth knowing before debugging a missing artifact:

- **An artifact written on daniel-server takes up to 5 minutes to appear.** That is the interval,
  not a fault. A session on daniel-box mounts its own tree directly and never waits.
- **The cron runs as `sys_user`, not root**, and only on `claude_code_artifacts_node`
  (daniel-box). An `artifacts` deploy neither renders the script nor installs the cron.
- **Every failed run goes to the journal, and a sustained outage adds one more line.** Read it
  with `journalctl -t sync-artifacts` on daniel-box. Every failed run logs there, with the
  consecutive-failure count, and a recovery logs the streak it ended. The run that reaches
  `claude_code_artifacts_sync_alert_after_failures` consecutive failures is the only one that
  writes to stderr. The cron pipes that stderr to the same tag rather than to mail (#6162).
  The script's `DECIDED:` comment has why (#2467).
- **The ssh transport passes `ClearAllForwardings=yes`.** `ssh` reads `~/.ssh/config` for every
  connection, so a `LocalForward` on the peer's host entry applies to this one too, and a bind
  collision on its local port fails the whole connection. rsync needs no forward, so the sync
  declines all of them.
- **The `Artifacts Peer Sync` Kuma tile is the monitor (#2516).** Every run pushes one verdict
  for all peers, after the loop. A peer at `claude_code_artifacts_sync_alert_after_failures`
  consecutive failures or more turns it DOWN, and it stays DOWN on every later run until the peer recovers.
  A shorter streak pushes UP and names the streak in the message. Ten minutes of silence means
  the cron itself stopped. The token is `artifacts_sync_push_token`, so the installed script is
  `0700` owned by `sys_user` — don't `cat` it on the host.
- **There is deliberately no file-age staleness check.** rsync copies the peer's own mtimes, so
  the newest file's age measures how often sessions on the peer write artifacts, not whether
  the sync works. A successful `rsync --delete` leaves the mirror equal to the peer, so a green
  tile means an old peer artifact really is the newest one the peer has. The script's `DECIDED:`
  comment carries the reasoning.

## A new Python module goes in `artifacts_modules`, and nowhere else
The pod runs `python3 /app/artifact_server.py` against the whole ConfigMap mounted at `/app`,
and `templates/configmap.yaml.j2` builds one key per entry in `artifacts_modules`
(`defaults/main.yml`). **Adding a file to `files/` without adding its name there ships nothing**
— the pod dies at import with `ModuleNotFoundError`, which no Ansible output reports.
`ansible/tests/services/test_artifacts_configmap.py` compares the list against `files/` in
both directions.

**Edit the list, not the template.** A comment in that template has crash-looped the pod twice.
The template's own header records both cases, and is the thing to read before editing it.

## Retiring a service does not remove it from the index
`artifacts_retired_services` exists so an artifact *about* a retired service stays findable by
name — a document reviewing the kopia retirement is only searchable if `kopia` is still a
recognised term. **Retiring a service should ADD its name there**, not drop it from
`artifacts_platform_services`. That is the opposite of the usual cleanup instinct, which is why
it is called out here.

## Pruning
Artifacts age out after 30 days without an update, so a doc that keeps being refreshed as work
lands stays put and an abandoned one clears itself. **Executable files are never pruned** — a
generated script is a tool, not a report. If a session writes one here it must be `chmod +x` or
it ages out with the docs.

**Starred artifacts are never pruned either.** The GUI's star writes `stars.txt` in
`artifacts_stars_dir`, the pod's only writable mount, and each host's prune reads its own copy.
`docs/artifacts-star-list.md` covers how the list reaches each host, and the window before it does.
