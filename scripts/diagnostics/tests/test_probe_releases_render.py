"""The render digest outranks the `--stale-only` path verdict, both ways.

Every refusal is paired with the clearing case it differs from by one field, so a rule that
stopped refusing fails here rather than reading green. The three-branch ladder -- a match
clears, a mismatch flags, an untrustworthy record falls back to the path rule -- gets one test
per branch on top of that.

Run: uv run pytest scripts/diagnostics/tests/test_probe_releases_render.py
"""

import json
import re
from types import SimpleNamespace

import pytest

from diagnostics.probe_lib import releases as pr
from diagnostics.probe_lib import releases_format as rf
from diagnostics.probe_lib import releases_render as rr

from _release_fixtures import _commit, _init_repo, _record, _set_origin_master

from lib.repo_paths import REPO


def test_render_dir_matches_the_ansible_default():
    defaults = (REPO / "ansible/roles/k8s/manifests/defaults/main.yml").read_text()
    m = re.search(r"^manifests_render_record_dir:\s*(\S+)\s*$", defaults, re.M)
    assert m, (
        "manifests_render_record_dir is not defined in the manifests role defaults"
    )
    assert m.group(1) == str(rr.RENDER_DIR)


def _release(service, commit, secrets=(), host="daniel-box", secret_digest=None):
    rec = _record(service, commit=commit)
    rec["host"] = host
    rec["secret_manifests"] = list(secrets)
    if secret_digest is not None:
        rec["secret_digest"] = secret_digest
    return rec


def _render(
    service,
    commit,
    secrets=(),
    digest="deadbeef",
    dirty=False,
    host="daniel-box",
    secret_digest=None,
):
    rec = {
        "service": service,
        "commit": commit,
        "tree_dirty": dirty,
        "host": host,
        "manifests_digest": digest,
        "secret_manifests": list(secrets),
    }
    if secret_digest is not None:
        rec["secret_digest"] = secret_digest
    return rec


def _write_renders(render_dir, *renders):
    render_dir.mkdir()
    for rec in renders:
        (render_dir / f"{rec['service']}.json").write_text(json.dumps(rec))
    return render_dir


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


def test_digest_provable_roles_match_the_deployers():
    import deploy_k8s_owed

    assert rr.DIGEST_PROVABLE_ROLES == deploy_k8s_owed.DIGEST_PROVABLE_ROLES


TIP = "b" * 40


def test_a_trusted_render_proves_current_is_clean():
    assert rr.render_proves_current(_release("x", "a" * 40), _render("x", TIP), TIP)


SECRET = ["s.yaml"]


def test_a_trusted_render_with_matching_secret_digests_is_clean():
    assert rr.render_proves_current(
        _release("x", "a" * 40, secrets=SECRET, secret_digest="d1"),
        _render("x", TIP, secrets=SECRET, secret_digest="d1"),
        TIP,
    )


@pytest.mark.parametrize(
    ("release", "render"),
    [
        pytest.param(
            _release("x", "a" * 40), _render("x", "c" * 40), id="other-commit"
        ),
        pytest.param(
            _release("x", "a" * 40), _render("x", TIP, dirty=True), id="dirty"
        ),
        pytest.param(
            _release("x", "a" * 40),
            _render("x", TIP, dirty="False"),
            id="dirty-as-string",
        ),
        pytest.param(
            _release("x", "a" * 40), _render("x", TIP, host="daniel-server"), id="host"
        ),
        pytest.param(
            _release("x", "a" * 40, host=None), _render("x", TIP), id="no-host"
        ),
        pytest.param(
            _release("x", "a" * 40),
            _render("x", TIP, secrets=["s.yaml"]),
            id="render-secret",
        ),
        pytest.param(
            _release("x", "a" * 40, secrets=["s.yaml"]),
            _render("x", TIP),
            id="release-secret",
        ),
        pytest.param(_release("x", "a" * 40), None, id="no-render"),
        pytest.param(
            _release("x", "a" * 40, secrets=SECRET, secret_digest="d1"),
            _render("x", TIP, secrets=SECRET, secret_digest="d2"),
            id="secret-digest-differs",
        ),
        pytest.param(
            _release("x", "a" * 40, secrets=SECRET),
            _render("x", TIP, secrets=SECRET, secret_digest="d1"),
            id="release-predates-secret-digest",
        ),
        pytest.param(
            _release("x", "a" * 40, secrets=SECRET, secret_digest="d1"),
            _render("x", TIP, secrets=SECRET),
            id="render-predates-secret-digest",
        ),
        pytest.param(
            _release("x", "a" * 40, secrets=SECRET, secret_digest=""),
            _render("x", TIP, secrets=SECRET, secret_digest=""),
            id="both-keyless",
        ),
        pytest.param(
            _release("x", "a" * 40, secrets=SECRET, secret_digest="d1"),
            _render("x", TIP, secrets=["t.yaml"], secret_digest="d1"),
            id="secret-names-differ",
        ),
    ],
)
def test_an_untrusted_render_is_flagged(release, render):
    assert not rr.render_proves_current(release, render, TIP)


def test_an_unresolved_ref_is_flagged():
    assert not rr.render_proves_current(
        _release("x", "a" * 40), _render("x", TIP), None
    )


@pytest.mark.parametrize(
    ("release", "render", "expected"),
    [
        pytest.param(
            _release("x", "a" * 40), _render("x", TIP), rr.CURRENT, id="current"
        ),
        pytest.param(
            _release("x", "a" * 40),
            _render("x", TIP, digest="other"),
            rr.DRIFTED,
            id="drifted",
        ),
        pytest.param(
            _release("x", "a" * 40, secrets=SECRET, secret_digest="d1"),
            _render("x", TIP, secrets=SECRET, secret_digest="d2"),
            rr.DRIFTED,
            id="drifted-secret-digest",
        ),
        pytest.param(
            _release("x", "a" * 40, secrets=SECRET),
            _render("x", TIP, secrets=SECRET),
            rr.UNKNOWN,
            id="unknown-no-secret-digest",
        ),
        pytest.param(
            _release("x", "a" * 40),
            _render("x", "c" * 40, digest="other"),
            rr.UNKNOWN,
            id="unknown-other-commit-despite-a-mismatch",
        ),
    ],
)
def test_the_verdict_ladder_names_its_branch(release, render, expected):
    """A mismatch an untrustworthy record reports is UNKNOWN, never DRIFTED."""
    assert rr.digest_verdict(release, render, TIP)[0] == expected


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
