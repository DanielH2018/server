# Review coverage ledgers

One file per `/homelab-review` run, `<date>.json`, written by the skill's step 5. It records
what each domain's reviewer did, so a domain nobody looked at reads as a hole rather than as
clean. `evals/review_outcomes.jsonl` counts what the run found; this directory records what the
run looked at.

## Shape

A JSON array with exactly one row per review domain. The domain names are the ones
`findings.py --domain` takes: `security`, `network`, `backup-observability`, `cicd`, `container`,
`docs`. `REVIEW_DOMAINS` in `scripts/dev/review_metrics.py` is the list the validator enforces.

| field | value |
|---|---|
| `date` | the run date, `YYYY-MM-DD` |
| `domain` | one of the six names above |
| `agent` | the agent dispatched for it, or `null` |
| `status` | `covered` (the agent returned findings or leads), `clean` (an explicit "nothing found" for the paths in `reviewed`), `hole` (the agent returned nothing, errored, or was never dispatched), `out_of_scope` (the operator scoped the run to a subset) |
| `reviewed` | repository paths or areas the agent named as reviewed; required for `covered` and `clean` |
| `findings` | fingerprints or issue numbers of confirmed findings; `covered` rows carry at least one finding or lead, every other status carries none |
| `leads` | fingerprints of `needs_validation` leads (a source-grounded finding blocked on a fact outside the repo) |
| `reason` | required on a `hole` row |
| `notes` | free text |

To check a ledger, run `uv run python scripts/dev/review_metrics.py --check-coverage
evals/review_coverage/<date>.json`; it exits 1 and prints each problem. The tests in
`scripts/dev/tests/test_review_metrics.py` validate every committed ledger.

The shape is adapted from the coverage ledger in `cloudflare/security-audit-skill`, cut down to
`homelab-review`'s own unit, the domain. The first ledger, `2026-09-17.json`, came from a scoped
run of that skill rather than from `homelab-review`, which is why four of its rows are
`out_of_scope`.
