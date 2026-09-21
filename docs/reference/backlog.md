---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-21 18:17 UTC
generated_sha: 80b72a462
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/backlog.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Backlog

Findings Claude confirmed and did not fix in the session that found them, filed through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has been seen three times carries **escalated** (the filing plus two re-observations) and needs a durable owner: a test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`. A row marked in the Verify-by column carries a description of how to check it in its issue body — run `findings.py verify --all` to print them. That command reports and runs nothing; closing stays with `findings.py close`.

| # | Severity | Kind | Domain | Finding | First seen | Re-observed | Claim | Verify-by |
|---|---|---|---|---|---|---|---|---|
| [#2146](https://github.com/DanielH2018/server/issues/2146) | medium | gap | cicd | Turn on pinDigests so the ~52 tag-only image pins get digests | 2026-09-19 | 0 | - | ✓ |
| [#2149](https://github.com/DanielH2018/server/issues/2149) | medium | gap | container | Make the code-server image build reproducible: digest, pinned extensions, npm, node, apt, pip | 2026-09-19 | 0 | - | ✓ |
| [#2151](https://github.com/DanielH2018/server/issues/2151) | medium | gap | cicd | Run every prek hook with uv run --frozen so the gate resolves like the deployer | 2026-09-19 | 0 | - | ✓ |
| [#2155](https://github.com/DanielH2018/server/issues/2155) | medium | improvement | container | Replace fifteen retries/delay polls with kubectl wait on the named condition | 2026-09-19 | 0 | - | ✓ |
| [#2157](https://github.com/DanielH2018/server/issues/2157) | medium | improvement | cicd | Sort every iteration that reaches committed JSON, rendered manifests or checksum annotations | 2026-09-19 | 0 | - | ✓ |
| [#2158](https://github.com/DanielH2018/server/issues/2158) | medium | improvement | cicd | Inject a fixed now into the ~15 tests that build fixtures from the live clock | 2026-09-19 | 0 | - | ✓ |
| [#2159](https://github.com/DanielH2018/server/issues/2159) | medium | gap | cicd | Stop four tests depending on the machine: the real dotfiles corpus, a hardcoded uv path, busy-waits, a live socket | 2026-09-19 | 0 | - | ✓ |
| [#1469](https://github.com/DanielH2018/server/issues/1469) | low | gap | container | Restore the Valheim mods disabled by the 1.0 break | 2026-09-09 | 0 | - | ✓ |
| [#2081](https://github.com/DanielH2018/server/issues/2081) | low | improvement | container | Apply the dockerd/containerd GOGC=off drop-ins on daniel-pi one a day and grade each the day after | 2026-09-18 | 0 | - | ✓ |
| [#2124](https://github.com/DanielH2018/server/issues/2124) | low | gap | security | CrowdSec agent drops Traefik access-log lines with UnmarshalJSON errors | 2026-09-19 | 0 | - | ✓ |
| [#2136](https://github.com/DanielH2018/server/issues/2136) | low | improvement | cicd | Route scripts/deploy_tools and scripts/dev raw git/gh subprocess calls through scripts/lib | 2026-09-19 | 0 | - | - |
| [#2137](https://github.com/DanielH2018/server/issues/2137) | low | improvement | cicd | Fold the repeated _tasks/_defaults census helpers in ansible/tests into _helpers.py | 2026-09-19 | 0 | - | - |
| [#2139](https://github.com/DanielH2018/server/issues/2139) | low | improvement | docs | Fact-support lint: 54 prose citations in role CLAUDE.md files fail on edit; deferred minors from slices 1-3 | 2026-09-19 | 0 | - | ✓ |
| [#2145](https://github.com/DanielH2018/server/issues/2145) | low | improvement | docs | Reword the eight timer-backed Kuma tile descriptions that still say cron | 2026-09-19 | 0 | - | ✓ |
| [#2150](https://github.com/DanielH2018/server/issues/2150) | low | gap | container | Pin homelab-mcp pip deps, the busybox migration image and Email-to-RSS's npm ci | 2026-09-19 | 0 | - | ✓ |
| [#2154](https://github.com/DanielH2018/server/issues/2154) | low | improvement | cicd | Use UTC stamps and LC_ALL=C sort in deploy.sh, gitops_tick.sh, gen_infra_map and the etcd drills | 2026-09-19 | 0 | - | ✓ |
| [#2156](https://github.com/DanielH2018/server/issues/2156) | low | improvement | cicd | Replace the deploy plane's sleep-polls with flock, kubectl wait and a bounded timeout | 2026-09-19 | 0 | - | ✓ |
| [#2161](https://github.com/DanielH2018/server/issues/2161) | low | improvement | home-assistant | Move the z2m-device-setting command sequence into a script the skill calls | 2026-09-19 | 0 | - | ✓ |
| [#2162](https://github.com/DanielH2018/server/issues/2162) | low | improvement | docs | Script the k3s-upgrade gates so a skipped gate is an exit code, not a missed paragraph | 2026-09-19 | 0 | - | ✓ |
| [#2163](https://github.com/DanielH2018/server/issues/2163) | low | improvement | cicd | Script the three hand-run passages of the renovate-prs skill | 2026-09-19 | 0 | - | ✓ |
| [#2164](https://github.com/DanielH2018/server/issues/2164) | low | improvement | docs | Make gitops_tick.sh's permission deterministic and collapse its duplicated classifier paragraph | 2026-09-19 | 0 | - | ✓ |
| [#2167](https://github.com/DanielH2018/server/issues/2167) | low | gap | cicd | Pin the three documented pairwise deploy orderings in a test | 2026-09-19 | 0 | - | ✓ |
| [#2168](https://github.com/DanielH2018/server/issues/2168) | low | gap | backup-observability | Test the UptimeRobot keyword rules against the four documented URLs | 2026-09-19 | 0 | - | ✓ |
| [#2169](https://github.com/DanielH2018/server/issues/2169) | low | improvement | docs | Generate the reviewer agents' don't-re-flag list from the findings register | 2026-09-19 | 0 | - | ✓ |
| [#2170](https://github.com/DanielH2018/server/issues/2170) | low | gap | cicd | Hook --skip-staleness-check (scoped past the staging gate) and check the PR author in land.sh --arm-merge | 2026-09-19 | 0 | - | ✓ |
| [#2192](https://github.com/DanielH2018/server/issues/2192) | low | gap | docs | Re-measure the Bash-only no-doc share after inject-nested-docs lands | 2026-09-21 | 0 | - | ✓ |
| [#2196](https://github.com/DanielH2018/server/issues/2196) | low | gap | docs | post-merge-automation.md quotes a gitops_deploy CLAUDE.md sentence that no longer exists | 2026-09-21 | 0 | - | ✓ |
| [#2198](https://github.com/DanielH2018/server/issues/2198) | low | improvement | cicd | Move auto-approve-readonly's classify onto claude_guard.segment.parse once the package splits &> as a redirect | 2026-09-21 | 0 | - | ✓ |
