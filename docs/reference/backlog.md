---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-06 06:17 UTC
generated_sha: 37375f37
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/backlog.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Backlog

Findings Claude confirmed and did not fix in the session that found them, filed through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has been seen three times carries **escalated** (the filing plus two re-observations) and needs a durable owner: a test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`. A row marked in the Verify-by column carries a read-only command in its issue body — run `findings.py verify --all` to re-check every one and close what now passes.

| # | Severity | Kind | Domain | Finding | First seen | Re-observed | Claim | Verify-by |
|---|---|---|---|---|---|---|---|---|
| [#1175](https://github.com/DanielH2018/server/issues/1175) | medium | gap | backup-observability | The full etcd restore has never been executed; the weekly drill is list-only | 2026-09-05 | 0 | - | - |
| [#1270](https://github.com/DanielH2018/server/issues/1270) | medium | improvement | cicd | Shard the pytest job across matrix shards — the remaining CI pole after the census fix | 2026-09-05 | 0 | - | - |
| [#1314](https://github.com/DanielH2018/server/issues/1314) | medium | gap | backup-observability | A [30d] Prometheus query silently returns ~11 days, so windowed derivations quote the wrong denominator — *no vetted remediation* | 2026-09-06 | 0 | - | - |
| [#1068](https://github.com/DanielH2018/server/issues/1068) | low | gap | cicd | k3s control-plane bumps need an operator-driven upgrade plan | 2026-09-04 | 0 | - | - |
| [#1186](https://github.com/DanielH2018/server/issues/1186) | low | gap | backup-observability | daniel-box exceeds its rated 90C CPU limit 9.3% of the time and nobody has decided whether that is acceptable | 2026-09-05 | 0 | - | - |
| [#1269](https://github.com/DanielH2018/server/issues/1269) | low | improvement | cicd | renovate config validator costs a job slot on every master push to re-validate an untouched file | 2026-09-05 | 0 | - | - |
| [#1288](https://github.com/DanielH2018/server/issues/1288) | low | improvement | backup-observability | Re-derive CLAUDE_CGROUP_STALL_MAX_PCT from seven days of history | 2026-09-06 | 0 | - | - |
| [#1313](https://github.com/DanielH2018/server/issues/1313) | low | gap | cicd | An unsatisfiable-but-runnable verify-by predicate is still undetected | 2026-09-06 | 0 | - | - |
| [#1316](https://github.com/DanielH2018/server/issues/1316) | low | improvement | docs | Landings dashboard description still calls pr=unknown a live argparse failure | 2026-09-06 | 0 | - | - |
| [#1317](https://github.com/DanielH2018/server/issues/1317) | low | gap | cicd | A denylist line lost from the deployer config disables the self-heal that would restore it | 2026-09-06 | 0 | - | - |
