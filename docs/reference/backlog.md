---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-19 18:17 UTC
generated_sha: 8cf1ba83c
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/backlog.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Backlog

Findings Claude confirmed and did not fix in the session that found them, filed through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has been seen three times carries **escalated** (the filing plus two re-observations) and needs a durable owner: a test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`. A row marked in the Verify-by column carries a description of how to check it in its issue body — run `findings.py verify --all` to print them. That command reports and runs nothing; closing stays with `findings.py close`.

| # | Severity | Kind | Domain | Finding | First seen | Re-observed | Claim | Verify-by |
|---|---|---|---|---|---|---|---|---|
| [#2125](https://github.com/DanielH2018/server/issues/2125) | high | gap | docs | Nested CLAUDE.md and .claude/rules never load on a Bash read, which auto mode prefers | 2026-09-19 | 0 | - | ✓ |
| [#2126](https://github.com/DanielH2018/server/issues/2126) | high | improvement | docs | monitor-bridge and gitops_deploy CLAUDE.md are incident ledgers loaded whole on every touch | 2026-09-19 | 0 | - | ✓ |
| [#2123](https://github.com/DanielH2018/server/issues/2123) | medium | gap | security | CrowdSec http-crawl-non_statics bans the operator's own authenticated remote session | 2026-09-19 | 0 | - | ✓ |
| [#2127](https://github.com/DanielH2018/server/issues/2127) | medium | improvement | docs | Root CLAUDE.md carries ~110 lines that govern one subtree; move them to path-scoped rules after the Bash injector lands | 2026-09-19 | 0 | - | ✓ |
| [#2128](https://github.com/DanielH2018/server/issues/2128) | medium | improvement | docs | Six root CLAUDE.md sections restate a longer skill or docs copy; shrink to the Shell Commands summary shape | 2026-09-19 | 0 | - | ✓ |
| [#2129](https://github.com/DanielH2018/server/issues/2129) | medium | improvement | docs | User-level CLAUDE.md opens with a 15-line changelog of the voice-style migration | 2026-09-19 | 0 | - | ✓ |
| [#2133](https://github.com/DanielH2018/server/issues/2133) | medium | improvement | cicd | Share prune_worktrees.py's pure readers with dotfiles as a claude-worktree package | 2026-09-19 | 0 | - | - |
| [#2134](https://github.com/DanielH2018/server/issues/2134) | medium | improvement | cicd | Consume claude_guard.segment.parse from the three remaining hook shell splitters | 2026-09-19 | 0 | - | - |
| [#1469](https://github.com/DanielH2018/server/issues/1469) | low | gap | container | Restore the Valheim mods disabled by the 1.0 break | 2026-09-09 | 0 | - | ✓ |
| [#2081](https://github.com/DanielH2018/server/issues/2081) | low | improvement | container | Apply the dockerd/containerd GOGC=off drop-ins on daniel-pi one a day and grade each the day after | 2026-09-18 | 0 | - | ✓ |
| [#2119](https://github.com/DanielH2018/server/issues/2119) | low | gap | cicd | findings.py next misses a Closes owner/repo#n reference and offers an issue whose PR is open | 2026-09-19 | 0 | - | - |
| [#2124](https://github.com/DanielH2018/server/issues/2124) | low | gap | security | CrowdSec agent drops Traefik access-log lines with UnmarshalJSON errors | 2026-09-19 | 0 | - | ✓ |
| [#2131](https://github.com/DanielH2018/server/issues/2131) | low | improvement | cicd | nvidia-smi has no server-side handler, so the shared-verdict replay does not exercise its guard | 2026-09-19 | 0 | - | ✓ |
| [#2135](https://github.com/DanielH2018/server/issues/2135) | low | improvement | cicd | Mark or align block-protected-bash's os.getcwd() fallback against claude_guard.hook.read_cwd | 2026-09-19 | 0 | - | - |
| [#2136](https://github.com/DanielH2018/server/issues/2136) | low | improvement | cicd | Route scripts/deploy_tools and scripts/dev raw git/gh subprocess calls through scripts/lib | 2026-09-19 | 0 | - | - |
| [#2137](https://github.com/DanielH2018/server/issues/2137) | low | improvement | cicd | Fold the repeated _tasks/_defaults census helpers in ansible/tests into _helpers.py | 2026-09-19 | 0 | - | - |
| [#2139](https://github.com/DanielH2018/server/issues/2139) | low | improvement | docs | Fact-support lint: 54 prose citations in role CLAUDE.md files fail on edit; deferred minors from slices 1-3 | 2026-09-19 | 0 | - | ✓ |
