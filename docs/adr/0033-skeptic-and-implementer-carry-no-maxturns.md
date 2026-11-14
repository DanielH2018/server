---
id: "0033"
title: The skeptic and implementer agents carry no turn cap
status: Accepted
date: 2026-11-14
governs: []
---

# ADR-0033: The skeptic and implementer agents carry no turn cap

## Status

Accepted. The two rulings were made in #4615 (skeptic) and #4652 and #4663 (implementer). The
measurements below lived in `.claude/skills/homelab-review/SKILL.md` until #8378 moved them
here, because every review run loaded them and the reviewer acts only on the rulings.

## Context

Agent-authoring guidance suggests a `maxTurns` of about 20 to bound a runaway subagent. A cap
costs something only if it stops runs that would have produced a good result. The question
for each agent type was whether its long runs produce worse results than its short ones. A cap
would cut those long runs, so it pays only if they are worse.

### skeptic

Measured from Loki on 2026-10-12 over 2026-09-16 to 10-12: 57 skeptic runs took p50 18, p90 28
and max 47 tool uses. The four full reviews dispatched 7, 13, 7 and 9 skeptics, one per verified
finding. The run with the longest skeptics (2026-09-24) produced the most refutations and
downgrades, so the data shows no quality cost from length. A cap at 20 would stop up to 18 of
57 skeptics before their verdict.

### implementer

**The join.** It pairs each run's length with its PR's first-CI verdict:

1. Loki's `subagent_completed` event is not a run, because a resumed agent emits one event per
   resumption, while its transcript holds the whole run (#4665). Each event is assigned to a
   transcript in its session. A transcript takes at most 4 events, which must sum to its
   tool-use count, each lie within 3 s of one of its messages, and include one at its last
   message. An event left over is a run of unmeasured length.
2. A run's length is its transcript's number of distinct `tool_use` ids, because one transcript
   repeats 226 of its 487 `tool_use` entries.
3. A run's PR is the earliest PR, opened after the run started, on the last non-`master` branch
   in its transcript.
4. The PR's verdict is the earliest check run per check name on the head commit CI first ran on
   after the PR opened (`check-runs?filter=all`, since the default `latest` shows a rerun's
   result). Any failed check is red, and a `cancelled` check with no failure is no verdict.
5. The join drops runs on `master`, runs on a branch that never opened a PR, runs that started
   after their PR opened (conflict and review fix-ups), and PRs with no verdict.

**2026-10-12 to 11-03, measured 2026-11-03.** Loki holds 501 implementer events, and 248
transcripts survive: 246 on daniel-box and 2 on daniel-server. 362 events match 239
transcripts. 139 events lack a transcript, 69 of them from 3 sessions with none; their median is
25 tool uses, so the unmeasured runs skew short. Over the 248 runs, tool uses are p50 64, p90
237 and max 608 (nearest rank). The join drops 4 runs on `master`, 95 on a branch with no PR and
17 fix-ups, leaving 132 runs on 82 PRs, of which 24 opened red on `prek` or `pytest`. Grouped
per PR by its longest run, the quartiles 14-35, 36-76, 77-167 and 168-608 opened red 4/21,
5/21, 9/20 and 6/20. The top quartile against the rest is 6/20 against 18/62, with a one-sided
Fisher p of 0.57 that the top quartile is redder. Per run the p is 0.70, and per PR by total tool uses
0.34. Four PRs (#6105, #6107, #6138, #6553) have a failed `prek` check inside a workflow run
that reads `cancelled`. Reading them as no verdict leaves 20 red PRs in 78 and a top quartile
of 4/19 (p 0.79), so the decision does not depend on that rule. A cap at 20 would stop 216 of
the 248 runs.

**What the decision rests on.** The 95 runs with no PR are a little longer than the joined ones
(p50 71 against 60, p90 252 against 213). The longest are D5 orchestrator-wave implementers,
whose work lands through the orchestrator's PR. The join cannot score them, so the decision
assumes they behave like the joined runs. Twenty-four red PRs rule out a large effect of length
on red CI, not a small one. The comparison shows co-occurrence: a long run may be long because
its task was hard.

**The first window was too small to decide.** Over 2026-09-16 to 10-12, 142 events and 94
transcripts joined to 66 runs on 21 PRs, and only #3113 opened red.

### Workflow subagents and other agent types, measured but undecided

Workflow subagents (#4651) emit no `subagent_completed` event, and their `api_request` events
carry no per-agent id. Loki labels a Workflow agent's requests
`agent:builtin:workflow-subagent` only when the script names no agent type. An agent given a
type, such as `Explore`, is labelled with that type and is distinguishable only by
`workflow_run_id`. The window holds four Workflow runs in two sessions. Three are
target-architecture runs in one session, whose 20,234 `workflow-subagent` requests share 14
prompt ids and 4 trace ids. The fourth is a fan-out verify run of 3 `Explore` agents (#3123's
session, 2026-10-01). The per-agent transcripts under
`~/.claude/projects/<project>/<session>/subagents/workflows/<run>/` do separate the agents, and
the three target-architecture runs' distinct request ids total 18,838 against Loki's 20,234.
Their 384 agents with a request took p50 43, p90 87 and max 171 requests (p50 53, p90 103 and
max 184 tool uses), and a `maxTurns` of 20 would stop 341 of them. The six Author-phase agents
ran longest, at p50 146 requests. The 3 `Explore` verify agents took 33, 44 and 44 requests. Of
all 387 agents, 308 are target-architecture Review-phase agents, so the distribution describes
those workflows rather than Workflow use in general. No outcome was measured, so a cap waits on
one.

Over the same window the p90 tool uses of the other agent types were general-purpose 101,
deep-review 96, security-review 51 and Explore 46. No outcome was measured for them either, so
they are undecided, not uncapped by decision.

## Decision

Neither `skeptic` nor `implementer` carries a `maxTurns` cap, and a review dispatches as many
skeptics as it has verified findings. Workflow subagents and every other agent type stay
undecided until an outcome is measured for them.

## Consequences

A runaway skeptic or implementer runs until the session's own budget stops it. The measured
cost of a cap is concrete: it would have stopped about a third of skeptics before their verdict
and 216 of 248 implementer runs, with no evidence that those runs were worse.

The rulings hold only while their revisit triggers stay unmet. Revisit the skeptic ruling if one
review dispatches more than 20 skeptics or skeptic p90 passes 50 tool uses. Revisit the
implementer ruling if the red rate of the top quartile reaches twice that of the other three.

## Governs

No code line enforces this: the decision is the absence of a `maxTurns:` key in
`.claude/agents/skeptic.md` and `.claude/agents/implementer.md`. The two `DECIDED:` bullets in
`.claude/skills/homelab-review/SKILL.md` step 6 point here.
