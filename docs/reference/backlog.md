---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-03 18:17 UTC
generated_sha: 4ee11a94
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/backlog.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Backlog

Findings Claude confirmed and did not fix in the session that found them, filed through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has been seen three times carries **escalated** (the filing plus two re-observations) and needs a durable owner: a test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`. A row marked in the Verify-by column carries a read-only command in its issue body — run `findings.py verify --all` to re-check every one and close what now passes.

| # | Severity | Kind | Domain | Finding | First seen | Re-observed | Verify-by |
|---|---|---|---|---|---|---|---|
| [#993](https://github.com/DanielH2018/server/issues/993) | medium | gap | backup-observability | Loki's server-side log discards are unmonitored — 161k lines lost while the deadman counted 1,020 | 2026-09-03 | 0 | - |
| [#994](https://github.com/DanielH2018/server/issues/994) | medium | gap | backup-observability | An uptime-kuma restart silently voids that cycle's whole push-deadman cohort | 2026-09-03 | 0 | - |
| [#997](https://github.com/DanielH2018/server/issues/997) | medium | gap | backup-observability | Ad-hoc backup deletion spends the B2 Class C cap and writes no ledger line | 2026-09-03 | 0 | - |
| [#995](https://github.com/DanielH2018/server/issues/995) | low | improvement | backup-observability | host_temp judges daniel-box's AMD Tctl sensor against a fallback limit that may not fit the chip | 2026-09-03 | 0 | - |
| [#996](https://github.com/DanielH2018/server/issues/996) | low | gap | backup-observability | The WAN speed test has a Kuma tile but no Prometheus series, so a degradation has no history | 2026-09-03 | 0 | - |
