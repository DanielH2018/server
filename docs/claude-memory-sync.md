# Claude memory store copy — daniel-box to daniel-server

`homelab-claude-memory-sync.timer` copies daniel-box's Claude memory store to daniel-server
every `claude_code_memory_sync_interval` (15 minutes). daniel-box's host package ships it from
the `setup/claude_code` role's templates, and the role's `CLAUDE.md` carries the contract. This page holds the reasoning and the checks.

## Why it exists (#3123)

Claude Code keeps one memory store per project per host, and nothing kept daniel-server's store in
step with daniel-box's. Fan-out sessions placed on daniel-server therefore loaded a stale store
whose index contradicted the root `CLAUDE.md`.

## The decision

The operator chose daniel-box as the source on 2026-10-01. The copy is one-way and overwriting:

- `rsync -a --delete` makes daniel-server's store match daniel-box's, including `archive/`. Without
  `--delete`, daniel-server would keep every entry daniel-box retired.
- daniel-server's store is read-only. `--chmod=a-w` strips every write bit from what the copy
  leaves there, directories included, so a session's write fails at once with
  `EACCES: permission denied`. Before 2026-11-14 the write succeeded, and the next run replaced
  or deleted the file without a message
  ([ADR-0031](adr/0031-each-kind-of-fact-has-one-stateless-detector.md)). A lesson learned on
  daniel-server belongs in daniel-box's store, a `CLAUDE.md` rule or a `findings.py` issue.

The read-only target needs no second step. rsync, running as the files' owner, adds the
owner's write bit back to a directory only while it changes that directory. Measured with
rsync 3.2.7 on 2026-11-14: a run that changed one file, deleted one and added two left every
file and directory without a write bit, and a run with nothing to copy printed nothing. A
target left writable, with a stray file in it, was emptied of the file and locked by the next
run. A session on a store with no write bit starts and reads the index as before (Claude Code
2.1.312, headless).

The contract (scope, mode, abort valve and evidence) is the *Autonomous-role contract
(`claude-memory-sync` ...)* section of `hosts/components/claude-code/CLAUDE.md`. The unit runs
as `sys_user`, so ssh uses that user's key and the far files keep their owner. The source is the
agent's store when `claude_code_user` is the agent, as `docs/claude-agent-user.md` (slice 4)
describes.

## Checking it

To confirm the two stores match, run this on daniel-box. It prints nothing when they match:

```bash
rsync -ani --delete --chmod=a-w \
  /home/ubuntu/.claude/projects/-home-ubuntu-server/memory/ \
  daniel-server:/home/ubuntu/.claude/projects/-home-ubuntu-server/memory/
```

Without `--chmod=a-w` the command lists every file, because the two stores differ in their
write bits by design.

To write to daniel-server's store by hand, which only a repair needs, run
`chmod -R u+w` on it first. The next run strips the write bits again.

The source path is `claude_code_memory_sync_dir`, which follows `claude_code_user`.
daniel-box's host_vars set `claude_code_user: ubuntu`, so the source is the store under `/home/ubuntu`. When
`claude_code_user` names the agent, the source becomes
`/var/lib/claude/.claude/projects/-var-lib-claude-server/memory/`. The target path is
`claude_code_memory_sync_target_dir`, the operator's store, whichever user is set. To read
both paths as the installed unit has them, run
`systemctl cat homelab-claude-memory-sync.service`; its `ExecStart` names source, then target.

To see what the last runs changed, run `journalctl -u homelab-claude-memory-sync -n 50`.

To turn the copy off, set `claude_code_memory_sync_enabled: false`; the contract's *Mode* line has the
mechanics. daniel-server keeps the store the last run wrote, still read-only. To let sessions
there write it again, run `chmod -R u+w` on the store once.
