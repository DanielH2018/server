---
name: scaffolding-delete-pass
description: Audit hooks, CLAUDE.md rules, skills and agents for scaffolding a newer model no longer needs, and propose removals. Use at a model upgrade, or when the config has grown and nobody can say why a rule is there. The presumption is deletion; a run that removes nothing must say what it checked.
---

# Scaffolding delete pass

Find the parts of this setup that exist to compensate for a model that no longer needs
compensating, and propose removing them. Every loop here adds — the memory ladder promotes,
`changelog-watch` proposes additions, review ledgers grow — and nothing takes away. This is the
counterpart pass.

Run it **at a model upgrade**. That is when the evidence changes and when a removal is most
likely to be safe. Running it on a calendar is worse: nothing has changed, so the pass has no
new information and degrades into re-arguing settled decisions.

## The sorting rule

Two buckets, and only one of them decays:

| Compounds — keep | Compensates — audit and expect to delete |
|---|---|
| Repo-specific facts no model can infer: where a file must be edited, which paths are generated, what a hostname means here, that a diff decrypts a secret. | Work that exists because the model got something wrong: retries around a flaky judgement, prose routing the model between tools it could pick itself, instructions repeating a thing it now does unprompted. |

The test for a rule: **could a competent stranger with full repo access derive this?** If no, it
compounds — it is a fact about your world. If yes, it compensates, and it is only earning its
place while the model still needs telling.

Current examples, as a calibration and not a verdict:

- `block-protected-edits`, `validate-compose`, the SOPS handling rules → compound. Nothing in the
  model's training says `containers/` is rendered onto a host, or that `.gitattributes` makes
  `git diff` print plaintext.
- `auto-mode-bridge`'s retry-on-classifier-denial, the CLAUDE.md prose choosing `jsonq` over
  `python3` and `gron` before `jq` → compensate. Re-test these against the new model.

## Procedure

### 1. Establish what the new model actually does

Before reading a single rule, run the thing each candidate rule guards against and see whether the
new model still fails it. A rule removed on a guess is how a regression ships; a rule removed
against a run is a measurement.

Where a rule has an executable counterpart, this is cheap: run the check, not the prose.

### 2. Get invocation counts

Judgement is not the discriminator here — use is. Query OTEL for the last 30 days:

- subagent invocations per agent name
- skill invocations per skill name
- memory citations per entry (`scripts/dev/memory_survey.py --transcript-days 30`)

Something never invoked in 30 days is a candidate. Something invoked daily is not, whatever you
think of it. This is the only measurable route out of a config nobody can justify line by line,
and the `otel-review` skill has the query shapes.

An agent or skill with zero invocations is not automatically dead — it may cover a rare case that
matters (disaster recovery, a yearly rotation). Name the case, or drop it.

### 3. Read the candidates in this order

Cheapest to reverse first, so a mistake costs least:

1. **CLAUDE.md prose** — a deleted paragraph is restored from git in seconds.
2. **Skills and agents** — check for a caller before removing; a skill named in another skill's
   text has a consumer that grep will find.
3. **Hooks** — these change what the harness *permits*, so a wrong removal fails open and silently.
   Removing a hook needs the paired evidence: the input it used to reject, and a run showing that
   input is now either impossible or rejected by something else.

### 4. Propose, do not apply

Emit one block per candidate:

```
### <what to remove>
- Location: <file:line or path>
- Bucket: compounds | compensates
- Why it was added: <the failure it was a response to, from git log or the file itself>
- Evidence it is no longer needed: <the run, the check, or the invocation count>
- Reversal: <the command that puts it back>
- Risk if wrong: <what silently breaks>
```

`Why it was added` is the field that does the work. A rule whose origin nobody can reconstruct is
the strongest deletion candidate in the file — but say so explicitly rather than deleting it
quietly, because "I could not find why" and "there is no reason" are different claims.

### 5. State the outcome honestly

A run that proposes nothing is a legitimate result, and it must still say what it examined and what
evidence held each item in place. "Nothing to remove" with no list is indistinguishable from not
having run the pass.

## Trigger

The natural trigger is a model upgrade, which `changelog-watch` already surfaces. When that skill
reports a new model, it should name this pass as a follow-up rather than only proposing additions
— an upgrade is the one moment when both directions are worth considering, and only one of them
currently has an owner.
