"""Tests for the repair commands scripts/secrets_mgmt/consumers.py prints.

`ansible/tests/k8s/test_secret_consumer_census.py` covers `tree_consumers` over the live tree.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_consumers.py
"""

from pathlib import Path

from secrets_mgmt.consumers import FLUX_PATH, consumer_commands


# DECIDED: no test here asserts a cross-host token is not badly overdue. One did, and on
# 2026-10-25 the calendar turned master and two unrelated PRs red in the same minute with no
# code change. Overdue is runtime state: SecretRotationAuditFailed pages the daily audit for any
# overdue token, cross-host ones included, and `cmd_audit --check` deliberately ignores it.
# `test_no_test_file_reads_the_live_clock` refuses the bare `today()` call it was built on.


# ── the Flux path for a k8s consumer (D3.4) ──
# ADR-0022 records how a value read by both planes rotates. Flux substitutes the value from the
# cluster file, so each k8s consumer's line names the operator's `write` and the landing. The
# fixture is a whole repo because `consumer_commands` takes `repo`.
_FIXTURE_SECRET = "fixture_issue_4983_api_key"


def _repo_with_two_k8s_consumers(tmp_path: Path) -> str:
    """A repo whose secret is rendered by the k8s roles `bento-pdf` and `sonarr`."""
    for role in ("bento-pdf", "sonarr"):
        template = tmp_path / "ansible/roles/k8s" / role / "templates/secret.yaml.j2"
        template.parent.mkdir(parents=True)
        template.write_text(f"apiKey: {{{{ {_FIXTURE_SECRET} }}}}\n")
    return str(tmp_path)


def test_consumer_commands_prints_the_flux_path_for_every_k8s_consumer(tmp_path):
    commands = consumer_commands(
        _FIXTURE_SECRET, repo=_repo_with_two_k8s_consumers(tmp_path)
    )

    assert commands == [FLUX_PATH % "bento-pdf", FLUX_PATH % "sonarr"]
    assert "`uv run python scripts/secrets_mgmt/cluster_vars.py write`" in commands[0]
