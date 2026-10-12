"""`promote.py` reads the deployer's required CI contexts from this repository's own tree.

The key moves at H7.2e, and its old home then holds a Jinja reference to the new one. A read
that kept pointing at the old file would get a string and refuse every promotion, so this
test runs the CLI's read against the checkout and fails the PR that moves the key without
repointing `promote.py`.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_ci_contexts.py
"""

import pytest

import promote
from lib.repo_paths import REPO

# The one context the role default lists at 70591c307, and still lists on origin/master.
NAMED_CONTEXT = "prek (lint + validate + tests + secrets)"


def test_the_checkout_holds_a_non_empty_list_naming_the_ci_job_is_clean():
    contexts = promote.read_ci_contexts(REPO / promote.CI_CONTEXTS_SOURCE)
    assert contexts and all(isinstance(name, str) and name for name in contexts)
    assert NAMED_CONTEXT in contexts


def test_a_jinja_reference_in_place_of_the_list_is_flagged(tmp_path):
    moved = tmp_path / "main.yml"
    moved.write_text('gitops_deploy_ci_contexts: "{{ ci_required_contexts }}"\n')
    with pytest.raises(promote.ReadError, match="non-empty list of non-empty strings"):
        promote.read_ci_contexts(moved)
