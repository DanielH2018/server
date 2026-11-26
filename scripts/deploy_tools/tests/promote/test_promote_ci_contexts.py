"""`promote.py` reads the required CI contexts from this repository's own tree.

`ci_required_contexts` lives in `estate/vars.yml`, and the deployer's role default is a Jinja
reference to it. A read of the role default would get a string and refuse every promotion, so
this test runs the CLI's read against the checkout and fails the PR that moves the key without
repointing `promote.py`. The red half writes a Jinja reference in place of the list, which is
what a read of the role default would see.

Run: uv run pytest scripts/deploy_tools/tests/promote/test_promote_ci_contexts.py
"""

import pytest

from deploy_tools import promote
from lib.repo_paths import REPO

# The one context the list holds.
NAMED_CONTEXT = "prek (lint + validate + tests + secrets)"


def test_the_checkout_holds_a_non_empty_list_naming_the_ci_job_is_clean():
    contexts = promote.read_ci_contexts(REPO / promote.CI_CONTEXTS_SOURCE)
    assert contexts and all(isinstance(name, str) and name for name in contexts)
    assert NAMED_CONTEXT in contexts


def test_a_jinja_reference_in_place_of_the_list_is_flagged(tmp_path):
    moved = tmp_path / "main.yml"
    moved.write_text('ci_required_contexts: "{{ other_contexts }}"\n')
    with pytest.raises(promote.ReadError, match="non-empty list of non-empty strings"):
        promote.read_ci_contexts(moved)
