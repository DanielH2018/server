"""A held fan-out phase gets `.claude/rules/` text, because `--setting-sources user` drops it.

G5 moves root `CLAUDE.md` text into path-scoped rules. A phase on held settings loads no
rules, so the pipeline appends them to the prompt (`held_hooks.rules_text`).
"""

from _review_fakes import PR, _pipeline, _report

CLEAN = {"summary": "", "findings": []}


def _prompt_of(argv):
    return argv[argv.index("--append-system-prompt") + 1]


def _reviewer_prompt(tmp_path, changed):
    pipeline, run = _pipeline(
        tmp_path, [_report(f"Opened {PR}"), _report(structured=CLEAN)]
    )
    run.changed_paths = changed
    pipeline.run_all()
    assert run.claude[1][2] == "review"
    return pipeline, _prompt_of(run.claude[1][0])


def test_a_range_touching_secrets_puts_the_secrets_rule_in_the_reviewers_prompt(
    tmp_path,
):
    pipeline, prompt = _reviewer_prompt(tmp_path, ["secrets/x.yml"])
    assert pipeline.rules["secrets.md"][1] in prompt


def test_a_range_touching_no_ruled_path_puts_no_rule_text_in_the_reviewers_prompt(
    tmp_path,
):
    # Not `docs/x.md`: `generated-docs.md` has governed `docs/**` since the plan's baseline.
    pipeline, prompt = _reviewer_prompt(tmp_path, ["README.md"])
    assert pipeline.rules
    assert not [n for n, (_, text) in pipeline.rules.items() if text in prompt]
