"""A render record clears a `--stale-only` path hit only when it proves the bytes current (#2586).

Every refusal is paired with the clearing case it differs from by one field, so a rule that
stopped refusing fails here rather than reading green.

Run: uv run pytest scripts/diagnostics/tests/test_probe_releases_render.py
"""

import json
import re
from pathlib import Path

import pytest

from diagnostics.probe_lib import releases as pr
from diagnostics.probe_lib import releases_render as rr

from _release_fixtures import _commit, _init_repo, _record, _set_origin_master

REPO = Path(__file__).resolve().parents[3]


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
    cleared = rr.apply_renders(
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
    """uptime-kuma's static-monitors change left the digest identical on 2026-09-25.

    Records written before #2574 carry no `secret_digest`, so they keep the path verdict.
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
    """A monitor added to static-monitors.yaml moves `secret_digest`, not `manifests_digest`."""
    repo, base, tip = repo
    secrets = ["static-monitors.yaml"]
    stale, cleared = _stale_after_renders(
        repo,
        [_release("uptime-kuma", base, secrets=secrets, secret_digest="c0ffee")],
        [_render("uptime-kuma", tip, secrets=secrets, secret_digest="decade")],
        tmp_path,
    )
    assert "static-monitors.yaml.j2" in stale["uptime-kuma"]
    assert cleared == []


def test_a_different_digest_is_flagged(repo, tmp_path):
    repo, base, tip = repo
    stale, _ = _stale_after_renders(
        repo,
        [_release("littlelink", base)],
        [_render("littlelink", tip, digest="cafef00d")],
        tmp_path,
    )
    assert "littlelink" in stale


def test_a_matched_service_inside_the_grace_window_is_not_pending(repo, tmp_path):
    repo, base, tip = repo
    stale, pending = {}, {"littlelink": 60}
    rr.apply_renders(
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
