"""The render digest outranks the `--stale-only` path verdict, both ways, over real git history.

Most tests drive `compute_stale` and then `apply_digest_verdicts` over a throwaway repo. The
three-branch ladder -- a match clears, a mismatch flags, an untrustworthy record falls back to
the path rule -- gets one test per branch, and so does a render of a commit behind the ref
(#4881). The single-pair refusal table is `test_probe_releases_render_ladder.py`.

Run: uv run pytest scripts/diagnostics/tests/test_probe_releases_render.py
"""

import re
from types import SimpleNamespace

import pytest

from diagnostics.probe_lib import releases as pr
from diagnostics.probe_lib import releases_format as rf
from diagnostics.probe_lib import releases_render as rr

from _release_fixtures import (
    _commit,
    _init_repo,
    _release,
    _render,
    _set_origin_master,
    _write_renders,
)
from lib.git_testing import git_out

from lib.repo_paths import REPO


def test_render_dir_matches_the_ansible_default():
    defaults = (REPO / "ansible/roles/k8s/manifests/defaults/main.yml").read_text()
    m = re.search(r"^manifests_render_record_dir:\s*(\S+)\s*$", defaults, re.M)
    assert m, (
        "manifests_render_record_dir is not defined in the manifests role defaults"
    )
    assert m.group(1) == str(rr.RENDER_DIR)


@pytest.fixture
def repo(tmp_path):
    """littlelink and uptime-kuma applied at `base`; a template of each changed at `tip`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(
        repo,
        {
            "ansible/roles/k8s/littlelink/templates/service.yaml.j2": "v1\n",
            "ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2": "v1\n",
        },
        "v1",
    )
    tip = _commit(
        repo,
        {
            "ansible/roles/k8s/littlelink/templates/service.yaml.j2": "v2\n",
            "ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2": "v2\n",
        },
        "v2",
    )
    _set_origin_master(repo, tip)
    return repo, base, tip


def _stale_after_renders(repo, records, renders, tmp_path, pending=None):
    stale = pr.compute_stale(records, repo_root=repo, shared_roles=set())
    cleared = rr.apply_digest_verdicts(
        stale,
        records,
        pending=pending,
        repo_root=repo,
        render_dir=_write_renders(tmp_path / "renders", *renders),
    )
    return stale, cleared


def test_a_matching_digest_with_no_secret_manifests_is_clean(repo, tmp_path):
    repo, base, tip = repo
    stale, cleared = _stale_after_renders(
        repo, [_release("littlelink", base)], [_render("littlelink", tip)], tmp_path
    )
    assert stale == {}
    assert cleared == ["littlelink"]


def test_a_secret_manifest_without_a_secret_digest_falls_back_to_the_path_rule(
    repo, tmp_path
):
    """uptime-kuma's static-monitors change leaves the digest identical.

    Records without a `secret_digest` keep the path verdict.
    """
    repo, base, tip = repo
    secrets = ["static-monitors.yaml"]
    stale, cleared = _stale_after_renders(
        repo,
        [_release("uptime-kuma", base, secrets=secrets)],
        [_render("uptime-kuma", tip, secrets=secrets)],
        tmp_path,
    )
    assert "static-monitors.yaml.j2" in stale["uptime-kuma"]
    assert cleared == []


def test_a_matching_secret_digest_clears_a_secret_manifest(repo, tmp_path):
    repo, base, tip = repo
    secrets = ["static-monitors.yaml"]
    stale, cleared = _stale_after_renders(
        repo,
        [_release("uptime-kuma", base, secrets=secrets, secret_digest="c0ffee")],
        [_render("uptime-kuma", tip, secrets=secrets, secret_digest="c0ffee")],
        tmp_path,
    )
    assert stale == {}
    assert cleared == ["uptime-kuma"]


def test_a_moved_secret_digest_is_flagged(repo, tmp_path):
    """A monitor added to static-monitors.yaml moves `secret_digest`, not `manifests_digest`.

    The reason is the digest verdict rather than the path hit beside it.
    """
    repo, base, tip = repo
    secrets = ["static-monitors.yaml"]
    stale, cleared = _stale_after_renders(
        repo,
        [_release("uptime-kuma", base, secrets=secrets, secret_digest="c0ffee")],
        [_render("uptime-kuma", tip, secrets=secrets, secret_digest="decade")],
        tmp_path,
    )
    assert "applied secret manifests" in stale["uptime-kuma"]
    assert cleared == []


def test_a_different_digest_is_reported_as_the_digest_verdict(repo, tmp_path):
    """The mismatch is the reason, not the path hit it happens to sit beside."""
    repo, base, tip = repo
    stale, _ = _stale_after_renders(
        repo,
        [_release("littlelink", base)],
        [_render("littlelink", tip, digest="cafef00d")],
        tmp_path,
    )
    assert stale["littlelink"].startswith(rr.DIGEST_REASON_PREFIX)
    assert "applied manifests" in stale["littlelink"]


def test_a_different_digest_flags_a_service_no_path_hit_reaches(repo, tmp_path):
    """The digest can make a service stale on its own.

    `uptime-kuma`'s own template moved at `tip`, `littlelink`'s did not -- so littlelink reads
    clean by every path rule, and only the digest can see that its applied bytes are not what
    origin/master renders.
    """
    repo, _, tip = repo
    records = [_release("littlelink", tip)]
    stale = pr.compute_stale(records, repo_root=repo, shared_roles=set())
    assert stale == {}, (
        "the path rules must read this service clean for the test to mean anything"
    )
    rr.apply_digest_verdicts(
        stale,
        records,
        repo_root=repo,
        render_dir=_write_renders(
            tmp_path / "renders", _render("littlelink", tip, digest="cafef00d")
        ),
    )
    assert stale["littlelink"].startswith(rr.DIGEST_REASON_PREFIX)


def test_a_moved_secret_digest_flags_a_service_no_path_hit_reaches(repo, tmp_path):
    """The uptime-kuma case, from the other side.

    A monitor added to a secret manifest leaves `manifests_digest` identical, so only
    `secret_digest` names the drift -- a differing one makes the service stale rather than
    merely declining to clear a path hit.
    """
    repo, _, tip = repo
    secrets = ["static-monitors.yaml"]
    records = [_release("littlelink", tip, secrets=secrets, secret_digest="c0ffee")]
    stale = pr.compute_stale(records, repo_root=repo, shared_roles=set())
    assert stale == {}
    rr.apply_digest_verdicts(
        stale,
        records,
        repo_root=repo,
        render_dir=_write_renders(
            tmp_path / "renders",
            _render("littlelink", tip, secrets=secrets, secret_digest="decade"),
        ),
    )
    assert "applied secret manifests" in stale["littlelink"]


def test_an_untrustworthy_record_leaves_the_path_verdict_alone(repo, tmp_path):
    """The third branch: a render from another commit answers nothing, either way."""
    repo, base, _tip = repo
    records = [_release("littlelink", base)]
    stale = pr.compute_stale(records, repo_root=repo, shared_roles=set())
    rr.apply_digest_verdicts(
        stale,
        records,
        repo_root=repo,
        render_dir=_write_renders(
            tmp_path / "renders", _render("littlelink", base, digest="cafef00d")
        ),
    )
    assert stale["littlelink"].startswith("changed since applied: ")


def test_a_drifted_service_inside_the_grace_window_keeps_waiting(repo, tmp_path):
    """A landing's own deploy is still in flight, which a digest cannot tell from drift."""
    repo, base, tip = repo
    stale, pending = {}, {"littlelink": 60}
    rr.apply_digest_verdicts(
        stale,
        [_release("littlelink", base)],
        pending=pending,
        repo_root=repo,
        render_dir=_write_renders(
            tmp_path / "renders", _render("littlelink", tip, digest="cafef00d")
        ),
    )
    assert stale == {}
    assert pending == {"littlelink": 60}


def test_a_drifted_digest_does_not_replace_the_unknown_commit_doubt(repo, tmp_path):
    """Provenance outranks the bytes: nobody can say where the applied bytes came from."""
    repo, _, tip = repo
    stale, _ = _stale_after_renders(
        repo,
        [_release("littlelink", "f" * 40)],
        [_render("littlelink", tip, digest="cafef00d")],
        tmp_path,
    )
    assert stale == {"littlelink": "commit unknown to this checkout"}


def test_a_matched_service_inside_the_grace_window_is_not_pending(repo, tmp_path):
    repo, base, tip = repo
    stale, pending = {}, {"littlelink": 60}
    rr.apply_digest_verdicts(
        stale,
        [_release("littlelink", base)],
        pending=pending,
        repo_root=repo,
        render_dir=_write_renders(tmp_path / "renders", _render("littlelink", tip)),
    )
    assert pending == {}


def test_an_unknown_commit_is_flagged_despite_a_match(repo, tmp_path):
    """A digest match clears path hits, not the doubt about where the bytes came from."""
    repo, _, tip = repo
    stale, _ = _stale_after_renders(
        repo, [_release("littlelink", "f" * 40)], [_render("littlelink", tip)], tmp_path
    )
    assert stale == {"littlelink": "commit unknown to this checkout"}


_OWN = "ansible/roles/k8s/littlelink/templates/service.yaml.j2"
_PVC = "ansible/roles/k8s/volume-claim/templates/pvc.yaml.j2"
_RENDERER = "ansible/roles/k8s/manifests/tasks/apply.yml"


def _shared_role_stale(tmp_path, changed, pending=None):
    """littlelink applied at `base`, `changed` moved by `tip`, and a render at `tip` matching."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(repo, {p: "v1\n" for p in (_OWN, _PVC, _RENDERER)}, "v1")
    _set_origin_master(repo, _commit(repo, {p: "v2\n" for p in changed}, "v2"))
    records, hits = [_release("littlelink", base)], {}
    tip = rr.resolve_ref("origin/master", repo)
    kwargs = {"pending": pending, "grace_seconds": 3600} if pending is not None else {}
    stale = pr.compute_stale(
        records,
        repo_root=repo,
        shared_roles={"volume-claim", "manifests"},
        consumers={},
        hits_out=hits,
        **kwargs,
    )
    rr.apply_digest_verdicts(
        stale,
        records,
        pending=pending,
        repo_root=repo,
        render_dir=_write_renders(tmp_path / "renders", _render("littlelink", tip)),
        hits=hits,
    )
    return stale


@pytest.mark.parametrize("changed", [[_RENDERER], [_OWN, _RENDERER]])
def test_a_matching_digest_clears_a_renderer_hit_is_clean(tmp_path, changed):
    assert _shared_role_stale(tmp_path, changed) == {}


@pytest.mark.parametrize("changed", [[_PVC], [_OWN, _PVC]])
def test_a_matching_digest_keeps_a_volume_claim_hit_is_flagged(tmp_path, changed):
    """The PVC is staged outside the digest, so CURRENT proves nothing about it."""
    assert _shared_role_stale(tmp_path, changed) == {
        "littlelink": rr.path_reason([_PVC])
    }


def test_a_matching_digest_keeps_a_volume_claim_hit_pending(tmp_path):
    pending = {}
    assert _shared_role_stale(tmp_path, [_PVC], pending=pending) == {}
    assert list(pending) == ["littlelink"]


_OTHER = "docs/unrelated.md"


def _behind_stale(
    tmp_path,
    at_r,
    after_r,
    digest="deadbeef",
    side=False,
    spell=lambda sha: sha,
    edit=lambda path: "v2\n",
    render_first=False,
    revert=False,
):
    """littlelink applied at `base`, `at_r` moved by R, `after_r` by the ref, a render at R.

    With `side`, the render names a child of R that master never merged. `spell` rewrites the
    commit the render records, for a spelling git would still resolve. `edit` gives each
    `at_r` path its text at R, and `revert` puts each back to v1 at the tip. With
    `render_first`, R is the v1 commit and the apply is a
    child of it, so `at_r` moves after both.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(repo, {p: "v1\n" for p in (_OWN, _PVC, _RENDERER, _OTHER)}, "v1")
    if render_first:
        r, base = base, _commit(repo, {_OTHER: "v2\n"}, "applied")
        _commit(repo, {p: edit(p) for p in at_r}, "edit")
    else:
        r = _commit(repo, {p: edit(p) for p in at_r}, "R")
    if side:
        r = git_out(repo, "commit-tree", "HEAD^{tree}", "-p", r, "-m", "side")
    _set_origin_master(
        repo,
        _commit(
            repo,
            {p: "v3\n" for p in (_OTHER, *after_r)}
            | ({p: "v1\n" for p in at_r} if revert else {}),
            "tip",
        ),
    )
    records, hits = [_release("littlelink", base)], {}
    stale = pr.compute_stale(
        records,
        repo_root=repo,
        shared_roles={"volume-claim", "manifests"},
        consumers={},
        hits_out=hits,
    )
    rr.apply_digest_verdicts(
        stale,
        records,
        repo_root=repo,
        render_dir=_write_renders(
            tmp_path / "renders", _render("littlelink", spell(r), digest=digest)
        ),
        hits=hits,
    )
    return stale


@pytest.mark.parametrize("at_r", [[_RENDERER], [_OWN, _RENDERER]])
def test_a_hit_a_matching_render_behind_the_ref_holds_is_clean(tmp_path, at_r):
    """#4881: a comment-only release_stamp.yml edit held the fleet stale for ten hours."""
    assert _behind_stale(tmp_path, at_r, []) == {}


@pytest.mark.parametrize("after_r", [[_OWN], [_RENDERER]])
def test_a_hit_after_a_matching_render_behind_the_ref_is_flagged(tmp_path, after_r):
    """Only the hits R..ref touched are left, including R's own path touched again."""
    assert _behind_stale(tmp_path, [_RENDERER], after_r) == {
        "littlelink": rr.path_reason(after_r)
    }


def test_a_mismatch_behind_the_ref_keeps_the_path_verdict_is_flagged(tmp_path):
    """R..ref may hold the fix, so a mismatch at R is UNKNOWN rather than DRIFTED."""
    assert _behind_stale(tmp_path, [_RENDERER], [], digest="cafef00d") == {
        "littlelink": rr.path_reason([_RENDERER])
    }


def _comment_only(path):
    """`path`'s v1 text with a comment added, in the comment syntax its reader skips."""
    return "{# note #}v1\n" if path.endswith(".j2") else "# note\nv1\n"


@pytest.mark.parametrize("at_r", [[_RENDERER], [_OWN, _RENDERER]])
def test_a_mismatch_behind_the_ref_no_hit_explains_is_flagged_as_unexplained(
    tmp_path, at_r
):
    """#5903: a comment-only release_stamp.yml edit was named as homelab-mcp's drift."""
    stale = _behind_stale(tmp_path, at_r, [], digest="cafef00d", edit=_comment_only)
    assert stale["littlelink"].startswith(rr.DIGEST_REASON_PREFIX)
    assert "no change since the apply explains it" in stale["littlelink"]


@pytest.mark.parametrize(
    ("at_r", "render_first"),
    [
        pytest.param([_PVC, _RENDERER], False, id="undigested-hit"),
        pytest.param([_RENDERER], True, id="render-older-than-apply"),
    ],
)
def test_a_mismatch_behind_the_ref_an_unseen_change_may_explain_keeps_the_path_verdict_is_flagged(
    tmp_path, at_r, render_first
):
    """A volume-claim hit acts outside the digest, and R..C is a range no hit names."""
    stale = _behind_stale(
        tmp_path,
        at_r,
        [],
        digest="cafef00d",
        edit=_comment_only,
        render_first=render_first,
    )
    assert stale == {"littlelink": rr.path_reason(sorted(at_r))}


def test_a_mismatch_behind_the_ref_from_an_edit_the_tip_reverts_is_flagged(tmp_path):
    """apply.yml's edit at R explains R's mismatch, though C and the tip agree on it."""
    stale = _behind_stale(tmp_path, [_RENDERER], [], digest="cafef00d", revert=True)
    assert stale == {"littlelink": rr.path_reason([_RENDERER])}


def test_a_volume_claim_hit_a_render_behind_the_ref_holds_is_flagged(tmp_path):
    assert _behind_stale(tmp_path, [_PVC, _RENDERER], []) == {
        "littlelink": rr.path_reason([_PVC])
    }


def test_a_render_off_the_refs_history_keeps_the_path_verdict_is_flagged(tmp_path):
    """A commit the ref does not hold proves nothing about it, whatever its digest."""
    assert _behind_stale(tmp_path, [_RENDERER], [], side=True) == {
        "littlelink": rr.path_reason([_RENDERER])
    }


@pytest.mark.parametrize(
    "spell",
    [
        pytest.param(str.upper, id="uppercase"),
        pytest.param(lambda sha: sha[:12], id="abbreviated"),
        pytest.param(lambda sha: "--version", id="option"),
    ],
)
def test_a_render_commit_that_is_not_a_full_lowercase_sha_proves_nothing_is_flagged(
    tmp_path, spell
):
    """The record's commit reaches git, so a spelling git would resolve is still refused."""
    assert _behind_stale(tmp_path, [_RENDERER], [], spell=spell) == {
        "littlelink": rr.path_reason([_RENDERER])
    }


def test_digest_provable_roles_match_the_deployers():
    from deployer_lib import deploy_k8s_owed

    assert rr.DIGEST_PROVABLE_ROLES == deploy_k8s_owed.DIGEST_PROVABLE_ROLES


@pytest.mark.parametrize(
    ("ns", "expected"),
    [
        pytest.param(SimpleNamespace(service=None, previous=False), True, id="table"),
        pytest.param(
            SimpleNamespace(service=None, previous=True), False, id="previous"
        ),
        pytest.param(
            SimpleNamespace(service="littlelink", previous=False), False, id="service"
        ),
    ],
)
def test_only_the_flags_table_view_is_judged_at_all(ns, expected):
    """`--previous` reads the release BEFORE the current one.

    Its digest differs from what the ref renders by definition, so a digest verdict there
    would mark the whole fleet drifted for having been superseded. `run_releases` reads this
    once and gates BOTH verdicts on it.
    """
    assert rf.renders_flags_table(ns) is expected
