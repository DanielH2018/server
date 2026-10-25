"""`promote.py` reads the required CI contexts from this repository's own tree.

H7.2e moved the list from the deployer's role defaults to `ci_required_contexts` in
`group_vars/all.yml`, and the role default became a Jinja reference to it. A read that kept
pointing at the old file would get a string and refuse every promotion, so this test runs the
CLI's read against the checkout and fails the PR that moves the key without repointing
`promote.py`. The red half reads the deployer's role default, which is what a read left on the
old file would see.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_ci_contexts.py
"""

import pytest

import promote
from lib.repo_paths import REPO

# The one context the list holds.
NAMED_CONTEXT = "prek (lint + validate + tests + secrets)"


def test_the_checkout_holds_a_non_empty_list_naming_the_ci_job_is_clean():
    contexts = promote.read_ci_contexts(REPO / promote.CI_CONTEXTS_SOURCE)
    assert contexts and all(isinstance(name, str) and name for name in contexts)
    assert NAMED_CONTEXT in contexts


def test_the_deployers_role_default_is_a_reference_and_is_flagged():
    old = REPO / "ansible/roles/setup/gitops_deploy/defaults/main.yml"
    with pytest.raises(promote.ReadError, match="non-empty list of non-empty strings"):
        promote.read_ci_contexts(old)


def test_a_jinja_reference_in_place_of_the_list_is_flagged(tmp_path):
    moved = tmp_path / "main.yml"
    moved.write_text('ci_required_contexts: "{{ gitops_deploy_ci_contexts }}"\n')
    with pytest.raises(promote.ReadError, match="non-empty list of non-empty strings"):
        promote.read_ci_contexts(moved)
