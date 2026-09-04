# artifacts — the Claude Code artifact browser, and the cross-host sync behind it

Serves the HTML/Markdown artifacts Claude Code sessions write to `~/.claude/artifacts/`, so a
report generated in a terminal session is readable in a browser behind Authelia. Written
2026-08-24: this role had no `CLAUDE.md` at all, and it was the only role under `roles/k8s/` in
that state — which mattered because it installs a **root-scheduled cross-host cron** that nothing
else in the tree documents.

## At a glance
- **Image:** `python:3.14-alpine` (`artifacts_k8s_image`) — serves a generated index plus the files.
- **Host:** pinned to `daniel-box` (`artifacts_k8s_node`), because it bind-mounts that host's own
  artifact tree.
- **Serves:** `artifacts_host_dir` (`~/.claude/artifacts`, daniel-box's own) and
  `artifacts_peer_dir` (`~/.claude/artifacts-peer`, everything pulled from peers).
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`, `platform: k8s`.
- **Deploy tag:** `--tags "artifacts"`.

## The peer sync is a pull, and the direction is not arbitrary
`tasks/main.yml` installs `/usr/local/bin/sync-artifacts.sh` and a cron
(`artifacts_sync_minute`, every 5 minutes) that rsyncs each entry in `artifacts_peer_sources`
onto daniel-box.

**It pulls; it never pushes.** The reason is written at the task and is worth repeating because
it looks backwards at first glance: the name `daniel-box` does not resolve from daniel-server
(verified 2026-08-19), and pushing would need an ssh key inside the cluster either way.
daniel-box already reaches daniel-server over ssh, so the sync runs from the side that works.
It is a host cron rather than a sidecar for the same reason — **the ssh credential stays on the
host and never enters a pod.**

Consequences worth knowing before debugging a missing artifact:

- **An artifact written on daniel-server takes up to 5 minutes to appear.** That is the interval,
  not a fault. A session on daniel-box mounts its own tree directly and never waits.
- **The cron runs as `sys_user`, not root**, and is gated `when: not k8s_dry_run` — so a
  `--dry-run` deploy renders the script but installs no cron.
- **Nothing alerts if the sync stops.** There is no heartbeat and no staleness check; a peer whose
  artifacts quietly stop arriving looks identical to a peer that wrote none. Treat a suspiciously
  old peer artifact as "check the cron on daniel-box" rather than "the peer wrote nothing".

## A new Python module goes in `artifacts_modules`, and nowhere else
The pod runs `python3 /app/artifact_server.py` against the whole ConfigMap mounted at `/app`,
and `templates/configmap.yaml.j2` builds one key per entry in `artifacts_modules`
(`defaults/main.yml`). **Adding a file to `files/` without adding its name there ships nothing**
— the pod dies at import with `ModuleNotFoundError`, which no Ansible output reports.
`ansible/tests/services/test_artifacts_configmap.py` compares the list against `files/` in
both directions.

**Edit the list, not the template.** That ConfigMap has crash-looped the pod twice, both times
from a comment rather than from a key: once from a comment closing tag written inside a
comment, which ended the comment early and rendered the remaining prose into the document as
YAML, and once from an indented comment above `known_services.json`, which pushed that key
inside the block scalar above it. The template's own header records both in full, and is the
thing to read before editing it.

## Retiring a service does not remove it from the index
`artifacts_retired_services` exists so an artifact *about* a retired service stays findable by
name — a document reviewing the kopia retirement is only searchable if `kopia` is still a
recognised term. **Retiring a service should ADD its name there**, not drop it from
`artifacts_platform_services`. That is the opposite of the usual cleanup instinct, which is why
it is called out here.

## Pruning
Artifacts age out after 7 days without an update, so a doc that keeps being refreshed as work
lands stays put and an abandoned one clears itself. **Executable files are never pruned** — a
generated script is a tool, not a report. If a session writes one here it must be `chmod +x` or
it ages out with the docs.
