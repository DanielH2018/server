# Claude memory store copy — daniel-box to daniel-server

`claude-memory-sync.timer` copies daniel-box's Claude memory store to daniel-server every
`claude_code_memory_sync_interval` (15 minutes). The `setup/claude_code` role installs it, and its
`CLAUDE.md` carries the contract. This page holds the reasoning and the checks.

## Why it exists (#3123)

Claude Code keeps one memory store per project per host, and nothing kept daniel-server's store in
step with daniel-box's. Fan-out sessions placed on daniel-server therefore loaded a stale store
whose index contradicted the root `CLAUDE.md`.

## The decision

The operator chose daniel-box as the source on 2026-10-01. The copy is one-way and overwriting:

- `rsync -a --delete` makes daniel-server's store match daniel-box's, including `archive/`. Without
  `--delete`, daniel-server would keep every entry daniel-box retired.
- A memory that a daniel-server session writes is replaced or deleted at the next run. Sessions
  there are expected to read the store, not write it. A lesson learned on daniel-server belongs in
  daniel-box's store, a `CLAUDE.md` rule or a `findings.py` issue.

The contract (scope, mode, abort valve and evidence) is the *Autonomous-role contract
(`claude-memory-sync` ...)* section of `ansible/roles/setup/claude_code/CLAUDE.md`. The unit runs
as `sys_user`, so ssh uses that user's key and the far files keep their owner. The source is the
agent's store when `claude_code_user` is the agent, as `docs/claude-agent-user.md` (slice 4)
describes.

## Checking it

To confirm the two stores match, run this on daniel-box. It prints nothing when they match:

```bash
rsync -ani --delete \
  /var/lib/claude/.claude/projects/-var-lib-claude-server/memory/ \
  daniel-server:.claude/projects/-home-ubuntu-server/memory/
```

To see what the last runs changed, run `journalctl -u claude-memory-sync -n 50`.

To turn the copy off, set `claude_code_memory_sync_enabled: false`; the contract's *Mode* line has the
mechanics. daniel-server keeps the store the last run wrote.
