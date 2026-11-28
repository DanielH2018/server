"""The value rows whose docs are a host component's `CLAUDE.md`, a rule or a skill."""

from docs.fragment_readers import module_constant
from docs.doc_values_lib.readers import (
    CARDINALS,
    ORDINALS,
    cardinal,
    constant,
    key,
    match,
    one,
    per,
    phrase,
    rows,
    soak_plus_grace,
    staged_count,
)

_GENERATED_DOCS = ".claude/rules/generated-docs.md"
_CLAUDE_CODE = "hosts/components/claude-code"
_CC_DOC = f"{_CLAUDE_CODE}/CLAUDE.md"
_RENOVATE_SKILL = ".claude/skills/renovate-prs/SKILL.md"
_WORKTREE_SKILL = ".claude/skills/worktree-cleanup/SKILL.md"
_TRIAGE_PINS = f"{_CLAUDE_CODE}/templates/claude-triage-pins.j2"


# source -> (doc, pattern, read) for each sentence that states a value from that source.
VALUES = rows(
    {
        "scripts/diagnostics/probe_lib/health_rollout.py": [
            (
                "docs/deploying.md",
                r"a (\d+)-second restart window",
                constant("RECENT_RESTART_SECONDS"),
            ),
            (
                "docs/claude-tooling.md",
                r"restarted in the last (\d+)s\.",
                constant("RECENT_RESTART_SECONDS"),
            ),
        ],
        "hosts/components/docs-refresh/templates/docs-refresh.sh.j2": [
            (
                _GENERATED_DOCS,
                r"^(\w+) paths: `docs/reference/`",
                staged_count([word.capitalize() for word in CARDINALS]),
            ),
            (
                _GENERATED_DOCS,
                r"^Adding a (\w+) means editing the cron's `git add`",
                staged_count(ORDINALS, offset=1),
            ),
        ],
        f"{_CLAUDE_CODE}/units/homelab-claude-guardrail-ack.timer.j2": [
            (
                _CC_DOC,
                r"on both hosts, (\w+) minutes\s+after each audit run",
                match(r"^OnCalendar=\S+ \*:(\d+)/", lambda v: cardinal(str(int(v)))),
            ),
        ],
        f"{_CLAUDE_CODE}/templates/sync-artifacts.sh.j2": [
            (_CC_DOC, r"(\d+)-second connect timeout", match(r"ConnectTimeout=(\d+)")),
        ],
        _TRIAGE_PINS: [
            (_CC_DOC, r"`\$(\d+)`, \d+ turns", match(r"set budget_usd = (\d+)")),
            (_CC_DOC, r"`\$\d+`, (\d+) turns", match(r"set max_turns = (\d+)")),
            (
                _CC_DOC,
                r"turns and a (\d+)-minute wall clock",
                match(r"set run_timeout_s = (\d+)", per(60)),
            ),
            (
                _CC_DOC,
                r"and (\w+) runs a UTC day",
                match(r"set daily_cap = (\d+)", cardinal),
            ),
        ],
        f"{_CLAUDE_CODE}/defaults/main.yml": [
            (
                _CC_DOC,
                r"holds for (\w+) polls",
                key("claude_code_triage_poll_streak", cardinal),
            ),
            (
                _CC_DOC,
                r"more than (\w+) falls",
                key("claude_code_triage_poll_storm_size", cardinal),
            ),
        ],
        f"{_CLAUDE_CODE}/units/homelab-claude-triage-poll-alert.service": [
            (
                _CC_DOC,
                r"pages at most once (an hour)",
                match(r"-mmin -(\d+)", phrase({"60": "an hour"})),
            ),
        ],
        "hosts/components/deploy-ui/files/deploy_ui.html": [
            (
                "hosts/components/deploy-ui/CLAUDE.md",
                r"polls each panel every (\d+) seconds",
                match(r"setInterval\(refresh,(\d+)\)", per(1000)),
            ),
            (
                "hosts/components/deploy-ui/CLAUDE.md",
                r"`/api/log` every (\d+) seconds",
                match(r"setInterval\(pollLog,(\d+)\)", per(1000)),
            ),
        ],
        "scripts/lib/exit_codes.py": [
            (_WORKTREE_SKILL, r"Exit (\d+) means the branch", constant("USAGE_ERROR")),
        ],
        "hosts/components/worktree-sweep/units/homelab-worktree-sweep.timer": [
            (
                _WORKTREE_SKILL,
                r"A (\w+) timer on daniel-box",
                match(r"^OnCalendar=([A-Z][a-z]{2}) ", lambda day: "weekly"),
            ),
        ],
        "hosts/components/renovate-notify/files/pending_logic.py": [
            (
                _RENOVATE_SKILL,
                r"(\d+) days for a digest bump",
                constant("DIGEST_SOAK_DAYS"),
            ),
            (
                _RENOVATE_SKILL,
                r"for a digest bump, (\d+) for a version",
                constant("VERSION_SOAK_DAYS"),
            ),
            (
                _RENOVATE_SKILL,
                r"which soak\s+(\d+) day \(",
                lambda module: one(
                    module_constant(module, "FAST_DIGEST_SOAK_DAYS").values(),
                    "fast soak",
                ),
            ),
            (
                _RENOVATE_SKILL,
                r"threshold is (\d+) days, not",
                soak_plus_grace("FAST_DIGEST_SOAK_DAYS"),
            ),
            (
                _RENOVATE_SKILL,
                r"threshold is \d+ days, not (\d+)\.",
                soak_plus_grace("DIGEST_SOAK_DAYS"),
            ),
            (
                _RENOVATE_SKILL,
                r"plus a (\d+)-day grace",
                constant("PENDING_GRACE_DAYS"),
            ),
        ],
    }
)
