---
name: audit-permissions
description: Audit the permission log and propose allowlist improvements. Use when reviewing which Bash commands or tools keep prompting for approval, or when tuning the Claude Code permission setup in .claude/settings.local.json.
---

Review the permission audit log and propose concrete improvements to the allowlist.

1. Run the audit script and read its output:

   `uv run python .claude/scripts/audit-permissions.py`

   (Add `--since YYYY-MM-DD` to scope to recent activity, or `--json` for raw data.)

2. If the script reports no data, tell the user the log is empty (the hooks may not have run yet — they take effect on the next session after install) and stop.

3. Summarize the findings for the user:
   - Overall prompt rate and per-tool breakdown (auto-approved vs. prompted).
   - The most-prompted commands — these are the allowlist gaps.
   - The **Suggested Bash allowlist rules** — already segment-aware (splits compound `a && b | c` commands, ignores quoted/heredoc bodies and env prefixes) and cross-referenced against the current `allow`/`deny`/`ask` tiers across **both** `.claude/settings.json` and `.claude/settings.local.json`, so already-covered and policy-blocked patterns are filtered out for you.
   - The **Left to prompt by design** section — commands that keep prompting because they contain a segment that is unsafe to blanket-allow (`rm`, `cd`, `find`, `awk`, `cat`, `sudo`, `curl`, …). These are *expected* prompts, not gaps; do not propose allowlisting them.

4. For the top suggested rules, propose specific edits to the `allow` list in **`.claude/settings.local.json`** (this repo keeps per-host permission tuning there; `.claude/settings.json` stays minimal and shared, and most read-only Bash is already auto-approved by the `auto-approve-readonly.py` hook, so genuine gaps are usually a small set). Prefer the narrowest rule that covers the pattern (e.g. `Bash(gh pr *)` over `Bash(gh *)`). The script already excludes anything in `deny`/`ask`, but still sanity-check each pick and call out anything risky to auto-approve (e.g. utilities that can redirect-overwrite files, or anything with an exec/delete vector). Note the repo convention: the `auto-approve-readonly.py` classifier already covers read-only pipelines, so a suggestion that keeps prompting may be one that hook deliberately leaves to prompt — check `.claude/hooks/auto-approve-readonly.py` before adding a blanket rule.

5. Present the proposed `settings.local.json` changes as a diff and ask for confirmation before editing. Only edit the file after the user approves.

6. After any edit, validate the JSON:

   `uv run python -c "import json; json.load(open('.claude/settings.local.json')); print('valid')"`

   and remind the user that permission changes take effect on the next Claude Code session.
