"""A PR whose only change under a role is that role's document, from mapper to verdict.

WHAT WENT WRONG (issue #1701). `land.sh --pr 1696` deployed n8n and registry, both healthy,
then exited 1 with `needs-manual-apply` for `ansible/roles/k8s/manifests/` -- whose only
changed file in that PR was CLAUDE.md. `manifests` has no `containers_list` entry, so
`shared_roles` named it and the remedy printed was a full `ansible/deploy.yml` run for prose.
The comment-only exemption could not reach it: that test reads YAML content lines, and a
Markdown file is not comments.

TWO PLACES A `.md` USED TO REACH A VERDICT, and both are covered here. `role_for` feeds
`shared_roles`, which is the k8s half; the broad prefixes are the setup half, which a `.md`
under `roles/setup/` still matches by path and only `quiet_paths` can drop.

THE VERDICT IS ASSERTED, not just `plane_note`. `plane_note` returning "" is not the
outcome -- `no_tag_outcome` reads it together with `self_applied`, and only their combination
decides between `nothing-to-deploy` (exit 0) and `needs-manual-apply` (exit 1). That
combination is the issue's own Verify-by.

Run: uv run pytest scripts/deploy_tools/tests/test_land_doc_change_reaches_a_verdict.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import land_tags
from _land_fakes import MERGE_SHA
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome

_DOC = "ansible/roles/k8s/manifests/CLAUDE.md"
_TASKS = "ansible/roles/k8s/manifests/tasks/main.yml"
_SETUP_DOC = "ansible/roles/setup/k3s/CLAUDE.md"
_SETUP_DEFAULTS = "ansible/roles/setup/k3s/defaults/main.yml"


def test_a_role_document_is_clean():
    """PR #1696's shape: the only change under a tag-less role is that role's CLAUDE.md."""
    assert land_tags.role_for(_DOC) is None
    assert land_tags.shared_roles([_DOC]) == []
    assert land_tags.plane_note([_DOC]) == ""
    assert land_tags.self_applied([_DOC]) is False


def test_a_role_task_file_is_flagged():
    """The reject half: the same role's tasks/ is code, and only a full deploy applies it."""
    assert land_tags.role_for(_TASKS) == "manifests"
    assert land_tags.shared_roles([_TASKS]) == ["manifests"]
    assert "manifests" in land_tags.plane_note([_TASKS])


def test_a_document_beside_a_real_change_does_not_excuse_it():
    """One quiet document must not take the role change it documents with it."""
    assert "manifests" in land_tags.plane_note([_DOC, _TASKS])


def test_a_setup_role_document_is_clean_whatever_the_range():
    """The broad half. A `.md` under `roles/setup/k3s/` matches the setup prefix by path, so
    only `quiet_paths` can drop it -- and it must do so with no usable range, since the docs
    answer asks nothing of a diff."""
    for range_ in ("", "..", "abc"):
        assert land_tags.quiet_paths([_SETUP_DOC], range_) == {_SETUP_DOC}, range_
    quiet = land_tags.quiet_paths([_SETUP_DOC], "")
    assert land_tags.plane_note([_SETUP_DOC], quiet=quiet) == ""
    assert land_tags.self_applied([_SETUP_DOC], quiet=quiet) is False


def test_a_setup_role_yaml_file_is_flagged_with_no_range():
    """The reject half: `defaults/main.yml` beside it is not a document, and with no range to
    read it as comment-only it stays owed to a hand."""
    files = [_SETUP_DOC, _SETUP_DEFAULTS]
    quiet = land_tags.quiet_paths(files, "")
    assert quiet == {_SETUP_DOC}
    assert "k3s-bringup.yml --tags k3s" in land_tags.plane_note(files, quiet=quiet)


def _verdict(landing, files: list[str]) -> Outcome:
    """The verdict `land.sh` reaches for a PR with this file list and no service tag."""
    ln, _ = landing(None)
    ln.merge_sha = MERGE_SHA
    quiet = land_tags.quiet_paths(files, "")
    ln.plane = land_tags.plane_note(files, quiet=quiet)
    ln.self_applied = land_tags.self_applied(files, quiet=quiet)
    ln.self_applied_command = land_tags.self_applied_command(files, quiet=quiet)
    ln.remaining_setup = ""
    with pytest.raises(Outcome) as exc:
        deploy.no_tag_outcome(ln)
    return exc.value


def test_a_doc_only_change_to_a_tagless_role_ends_nothing_to_deploy(landing):
    outcome = _verdict(landing, [_DOC])
    assert (outcome.verdict, outcome.rc) == ("nothing-to-deploy", 0)


def test_a_task_change_to_the_same_role_still_ends_needs_manual_apply(landing):
    outcome = _verdict(landing, [_TASKS])
    assert (outcome.verdict, outcome.rc) == ("needs-manual-apply", 1)
