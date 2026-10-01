# Claude memory store copy — daniel-box to daniel-server

`claude-memory-sync.timer` copies daniel-box's Claude memory store to daniel-server every
`claude_code_memory_sync_interval` (15 minutes). The `setup/claude_code` role installs it, and its
`CLAUDE.md` carries the contract. This page holds the reasoning and the checks.

## Why it exists (#3123)

Claude Code keeps one memory store per project per host, under
`~/.claude/projects/-home-ubuntu-server/memory/`. Fan-out sessions placed on daniel-server load
daniel-server's store, and nothing kept it in step with daniel-box's. On 2026-10-01 the two stores
shared 2 file names out of 85 and 66. daniel-server's store had not been written since 2026-08-03,
and its index told sessions to stay on master, which contradicts the root `CLAUDE.md` rule of one
worktree per session.

## The decision

The operator chose daniel-box as the source on 2026-10-01. The copy is one-way and overwriting:

- `rsync -a --delete` makes daniel-server's store match daniel-box's, including `archive/`. Without
  `--delete`, daniel-server would keep every entry daniel-box retired.
- A memory that a daniel-server session writes is replaced or deleted at the next run. Sessions
  there are expected to read the store, not write it. A lesson learned on daniel-server belongs in
  daniel-box's store, a `CLAUDE.md` rule or a `findings.py` issue.
- Before the first run, daniel-server's 38 entries that daniel-box lacked were re-verified against
  the repo and the true ones folded into daniel-box's store. A copy of daniel-server's old store is
  at `~/claude-memory-store-backup-2026-10-01.tgz` on daniel-server.

## How a run works

The unit runs as `{{ sys_user }}`, so ssh uses that user's key and the far files keep their owner.

1. `ExecCondition` skips the run unless the source `MEMORY.md` exists and is non-empty. An empty
   source with `--delete` would wipe daniel-server's store.
2. `rsync --itemize-changes` writes one journal line per file it changed or deleted. That journal
   is the only record of a daniel-server write the copy discarded.
3. A non-zero exit fires `claude-memory-sync-alert.service`, which posts to the same Discord webhook
   as `claude-rc-alert.service`.

## Checking it

To confirm the two stores match, run this on daniel-box. It prints nothing when they match:

```bash
rsync -ani --delete ~/.claude/projects/-home-ubuntu-server/memory/ \
  daniel-server:.claude/projects/-home-ubuntu-server/memory/
```

To see what the last runs changed, run `journalctl -u claude-memory-sync -n 50`.

## Turning it off

Set `claude_code_memory_sync_enabled: false` in daniel-box's host_vars and apply
`uv run ansible-playbook ansible/initial_setup.yml --tags claude_code`. The role stops and disables
the timer and removes its three units. daniel-server keeps the store the last run wrote.
