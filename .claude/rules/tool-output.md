---
paths:
  - "scripts/**"
  - "hosts/lib/**"
  - "hosts/components/**/files/**"
  - ".claude/hooks/**"
---

# Tool output — what a script prints

Agents run these tools, and every line a tool prints lands in an agent's context window and
in the operator's terminal. A line that repeats what the reader already has, or lists items
nobody acts on, costs context on every run. The 2026-11-09 audit measured where that cost
came from (#7892 to #7899). A tool you add or change follows the five rules below.

1. **Lead with one count line.** For example, `place status` prints `2 done, 1 running`, and
   `probe.py targets` prints `91/91 targets up`. `scripts/lib/progress.py:count_line` builds
   one from a list of states.
2. **Then list only the items that need attention, grouped by reason.** A healthy, done,
   landed or live item is counted, not listed. When many items share one reason, print the
   reason once and the item count, not the reason after every name. `promote.py land-wait`
   prints `promotion at 64d8b24e, before M (5 Kustomizations)`, not five names each with the
   same note.
3. **Put the full view behind a flag.** `--all` restores every row; `--waiting` and `--full`
   are the narrower cases (`findings.py next`, `findings.py show`). Name the flag in the count
   line when it hides something: `10 waiting (8 deferred, 2 blocked); --waiting to list`.
4. **A polling loop prints only when its line changes.** Route the line through
   `scripts/lib/progress.py:OnChange`. Don't hand-roll a `last` check: three loops did, and
   `land_lib/host_wait.py` printed one line 42 times in one landing before it moved to
   `OnChange`.
5. **A result another program reads is machine-readable.** Make it one terminal line in the
   established shape: probe's `PROBE-VERDICT: <state> (…)`, land's
   `LAND-RESULT: verdict=… cause=…`, or one compact JSON object (`flux_wait.py`). Grade on an
   exit code or that line, never on a sentence's wording. Rewording a message must not change
   what a caller decides: `detach_notify` once matched health's prose, and a reworded line
   could have read a failed deploy as skipped.

Two smaller rules:

- **`--json` prints compact JSON**: `json.dumps(rows)` with no `indent`. Only `jq` and agents
  read it, and indentation roughly doubles the tokens. A file written to disk for a person to
  diff keeps its indent. The `printed-json-is-compact` row of
  `tests/repo/test_census_rows_python.py` enforces this for one-line
  `print(json.dumps(…))` calls.
- **A subprocess's chatter stays out of your output.** Capture the stderr of `uv`, `gh` or
  `git` and print it only on failure, cut to its deciding lines. A raw multi-line error
  repeated every poll is noise. Print it once, as one line, and again only when it changes.

A failure still names its cause and the next command to run. A shorter success never
justifies a vaguer failure.
