"""The grace window on `probe.py releases --stale-only` -- a fresh merge is pending, not stale.

Split from `test_probe_releases_stale.py`, which covers WHICH paths invalidate a stamp; this
file covers WHEN a change starts to count. `--grace-minutes` exists because the monitor would
push DOWN for a merge whose own landing is mid-deploy.

Run: uv run pytest scripts/diagnostics/tests/test_probe_releases_grace.py
"""

import pytest

from diagnostics.probe_lib import releases as pr
from diagnostics.probe_lib import releases_render as rr
from lib.git_testing import git, git_out, scrubbed_env

from _release_fixtures import (
    _commit,
    _init_repo,
    _record,
    _release,
    _render,
    _set_origin_master,
    _write_renders,
)
from lib.proc_testing import run


def _committer_time(repo, sha):
    return int(git_out(repo, "show", "-s", "--format=%ct", sha))


def _stale_role_repo(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(
        repo, {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v1\n"}, "v1"
    )
    tip = _commit(
        repo, {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v2\n"}, "v2"
    )
    _set_origin_master(repo, tip)
    return repo, base, tip


def test_a_merge_inside_the_grace_window_is_pending_not_stale(tmp_path):
    repo, base, tip = _stale_role_repo(tmp_path)
    pending = {}
    stale = pr.compute_stale(
        [_record("sonarr", commit=base)],
        repo_root=repo,
        shared_roles=set(),
        grace_seconds=3600,
        now=_committer_time(repo, tip) + 600,
        pending=pending,
    )
    assert stale == {}
    assert pending == {"sonarr": 600}


def test_a_merge_older_than_the_grace_window_is_flagged(tmp_path):
    repo, base, tip = _stale_role_repo(tmp_path)
    pending = {}
    stale = pr.compute_stale(
        [_record("sonarr", commit=base)],
        repo_root=repo,
        shared_roles=set(),
        grace_seconds=3600,
        now=_committer_time(repo, tip) + 3601,
        pending=pending,
    )
    assert "sonarr" in stale
    assert pending == {}


def _commit_dated(repo, files, message, epoch):
    """`_commit`, with the committer clock pinned so a test can build an old commit."""
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    env = scrubbed_env(
        GIT_AUTHOR_DATE=f"@{epoch} +0000",
        GIT_COMMITTER_DATE=f"@{epoch} +0000",
    )
    git(repo, "add", "-A", env=env)
    git(repo, "commit", "-q", "-m", message, env=env)
    return run(
        ["git", "rev-parse", "HEAD"], cwd=repo, env=env, check=True
    ).stdout.strip()


def test_a_fresh_commit_on_old_drift_does_not_restart_the_window(tmp_path):
    """The window counts from the OLDEST un-applied change, not the newest.

    `roles/k8s/manifests/` is an offending path for the whole fleet, so dating drift by the
    newest commit would let one busy shared role keep every service pending forever.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    template = "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"
    base = _commit(repo, {template: "v1\n"}, "v1")
    fresh = _committer_time(repo, base)
    _commit_dated(repo, {template: "v2\n"}, "v2, two hours ago", fresh - 7200)
    tip = _commit_dated(repo, {template: "v3\n"}, "v3, two minutes ago", fresh - 120)
    _set_origin_master(repo, tip)

    pending = {}
    stale = pr.compute_stale(
        [_record("sonarr", commit=base)],
        repo_root=repo,
        shared_roles=set(),
        grace_seconds=3600,
        now=fresh,
        pending=pending,
    )
    assert "sonarr" in stale
    assert pending == {}


def test_the_default_grace_is_none(tmp_path):
    """The cron passes the window explicitly; a caller that does not gets the old contract."""
    repo, base, tip = _stale_role_repo(tmp_path)
    stale = pr.compute_stale(
        [_record("sonarr", commit=base)],
        repo_root=repo,
        shared_roles=set(),
        now=_committer_time(repo, tip),
    )
    assert "sonarr" in stale


def test_pending_services_are_named_on_an_up_and_do_not_move_the_exit_code():
    text, code = pr.format_stale_only(
        {}, missing=[], pending={"code-server": 720}, grace_seconds=3600
    )
    assert code == 0
    assert "code-server (12 min ago)" in text
    assert "60-min grace" in text
    kuma_text, kuma_code = pr.format_stale_kuma(
        {}, missing=[], pending={"code-server": 720}, grace_seconds=3600
    )
    assert kuma_code == 0
    assert "code-server (12 min ago)" in kuma_text


def test_names_out_writes_the_counted_set_sorted(tmp_path):
    """release-staleness-check re-alerts when this set changes."""
    out = tmp_path / "names"
    pr.write_counted_names(
        out, {"sonarr": "changed since applied: roles/k8s/sonarr/"}, ["authelia"]
    )
    assert out.read_text() == "authelia\nsonarr\n"


def test_names_out_is_empty_for_a_clean_fleet(tmp_path):
    out = tmp_path / "names"
    pr.write_counted_names(out, {}, [])
    assert out.read_text() == ""


_RENDERER = "ansible/roles/k8s/manifests/tasks/apply.yml"
_OWN = "ansible/roles/k8s/littlelink/templates/service.yaml.j2"
_PVC = "ansible/roles/k8s/volume-claim/templates/pvc.yaml.j2"


def _after_render_behind(tmp_path, steps, render_at):
    """littlelink applied at `base`, then each `(path, age)` of `steps` committed `age` ago.

    Returns `(stale, pending)` after a matching render of step `render_at`, with a one-hour
    grace window.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(repo, {p: "v1\n" for p in (_RENDERER, _OWN, _PVC)}, "v1")
    fresh = _committer_time(repo, base)
    shas = [
        _commit_dated(repo, {path: f"v{i + 2}\n"}, f"step {i}", fresh - age)
        for i, (path, age) in enumerate(steps)
    ]
    _set_origin_master(repo, shas[-1])

    records, pending, hits = [_release("littlelink", base)], {}, {}
    kwargs = {"grace_seconds": 3600, "now": fresh, "pending": pending}
    stale = pr.compute_stale(
        records,
        repo_root=repo,
        shared_roles={"manifests", "volume-claim"},
        consumers={},
        hits_out=hits,
        **kwargs,
    )
    assert "littlelink" in stale, "the oldest hit must date the drift before the render"
    render = _render("littlelink", shas[render_at])
    rr.apply_digest_verdicts(
        stale,
        records,
        repo_root=repo,
        render_dir=_write_renders(tmp_path / "renders", render),
        hits=hits,
        **kwargs,
    )
    return stale, pending


@pytest.mark.parametrize(
    ("after_age", "expected_stale"),
    [
        pytest.param(120, {}, id="young-hit-after-render-is-pending"),
        pytest.param(3700, {"littlelink": None}, id="old-hit-after-render-is-stale"),
    ],
)
def test_a_render_behind_the_ref_dates_the_hits_it_leaves(
    tmp_path, after_age, expected_stale
):
    """A hit the render holds no longer dates the drift, so the hit after it starts the clock.

    Without the re-dating, a two-hour-old hit the render already answers for would keep the
    service stale for a merge two minutes old (#4881).
    """
    stale, pending = _after_render_behind(
        tmp_path, [(_RENDERER, 7200), (_OWN, after_age)], render_at=0
    )
    assert stale == {svc: rr.path_reason([_OWN]) for svc in expected_stale}
    assert pending == ({} if expected_stale else {"littlelink": after_age})


def test_an_undigested_hit_touched_again_after_the_render_keeps_its_date_is_flagged(
    tmp_path,
):
    """R's render does not answer for the volume-claim change before it, so drift dates from there.

    Re-dating it from the touch after R would hide a two-hour-old unapplied PVC change as pending.
    """
    stale, pending = _after_render_behind(
        tmp_path, [(_PVC, 7200), (_OWN, 7000), (_PVC, 120)], render_at=1
    )
    assert stale == {"littlelink": rr.path_reason([_PVC])}
    assert pending == {}


def test_an_undigested_hit_touched_only_after_the_render_gets_grace(tmp_path):
    """R answers for none of the volume-claim change, but the change is two minutes old.

    Its own commit dates it, so the older renderer hit R holds does not (#4881).
    """
    stale, pending = _after_render_behind(
        tmp_path, [(_RENDERER, 7200), (_PVC, 120)], render_at=0
    )
    assert stale == {}
    assert pending == {"littlelink": 120}


@pytest.mark.parametrize(
    ("own_age", "expected_pending"),
    [
        pytest.param(1800, {"littlelink": 1800}, id="oldest-kept-hit-is-inside-grace"),
        pytest.param(3700, {}, id="oldest-kept-hit-is-outside-grace"),
    ],
)
def test_a_service_dates_from_its_oldest_kept_hit_across_both_kinds(
    tmp_path, own_age, expected_pending
):
    """A digested hit after R and an undigested one: the older of the two sets the date."""
    stale, pending = _after_render_behind(
        tmp_path, [(_RENDERER, 7200), (_OWN, own_age), (_PVC, 120)], render_at=0
    )
    assert pending == expected_pending
    assert stale == (
        {} if expected_pending else {"littlelink": rr.path_reason([_OWN, _PVC])}
    )
