"""claude_config_render against a real git mirror, real staged files and the shipped overlays.

chezmoi is a stub that copies the template it is asked for, so the render tests check which
commit's template reaches the stage, not chezmoi's template language.
"""

import json
import os
import subprocess
from pathlib import Path

import claude_config_render as cr
import claude_config_lock
import pytest
from lib.proc_testing import write_exec

COMPONENTS = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "config-render"
OVERLAYS = {
    "triage.json": COMPONENTS / "claude-code/files/claude-triage-settings-overlay.json",
    "renovate.json": COMPONENTS / "renovate-agent/files/claude-settings-overlay.json",
}
COMMIT = "a" * 40
OTHER = "b" * 40
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "PATH": os.environ["PATH"],
}
CHEZMOI_STUB = (
    'while [ $# -gt 0 ]; do case "$1" in --file) f=$2;; --output) o=$2;; esac; shift; done\n'
    'cp "$f" "$o"\n'
)


@pytest.fixture
def host(tmp_path):
    """A record, a stage holding the t2-1 render, the shipped overlays and an empty publish dir."""
    record = tmp_path / "verified.json"
    record.write_text(json.dumps({"tag": "t2-1", "commit": COMMIT}))
    overlays = tmp_path / "overlays"
    overlays.mkdir()
    for name, src in OVERLAYS.items():
        (overlays / name).write_text(src.read_text())
    stage = _stage(tmp_path / "stage", COMMIT)
    paths = {
        "record": record,
        "overlay_dir": overlays,
        "publish_dir": tmp_path / "published",
    }
    return stage, paths


def _stage(stage: Path, commit: str, render: str | None = None) -> Path:
    out = stage / "out"
    out.mkdir(parents=True)
    (out / "commit").write_text(commit + "\n")
    (out / "settings.base.json").write_text(
        render or (FIXTURES / "t2-1-settings.base.json").read_text()
    )
    return stage


def _published(paths, name: str) -> dict:
    return json.loads((paths["publish_dir"] / "current" / name).read_text())


def test_the_t2_1_render_publishes_the_settings_each_session_ran_before(host):
    """The golden files are the hand-kept settings each session passed with --settings."""
    stage, paths = host

    assert cr.publish(stage, **paths) == f"published {COMMIT[:12]} for renovate, triage"

    for name in OVERLAYS:
        expected = json.loads((FIXTURES / f"published-{name}").read_text())
        assert _published(paths, name) == expected, name
    assert _published(paths, "source.json") == {"commit": COMMIT}


def test_only_the_verified_commit_is_rendered(tmp_path):
    """dotfiles#967 I7: a commit past the verified tag never reaches the stage."""
    origin = tmp_path / "origin"
    origin.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(origin), *args],
            env=GIT_ENV, check=True, timeout=60, capture_output=True, text=True,
        ).stdout.strip()  # fmt: skip

    git("init", "-q", "-b", "main")
    template = origin / "home" / ".chezmoitemplates" / "settings.base.json"
    template.parent.mkdir(parents=True)
    (origin / ".chezmoiroot").write_text("home\n")
    template.write_text('{"tag": "verified"}')
    git("add", "-A")
    git("commit", "-q", "-m", "verified")
    verified = git("rev-parse", "HEAD")
    template.write_text('{"tag": "unverified"}')
    git("commit", "-q", "-am", "past the tag")
    record = tmp_path / "verified.json"
    record.write_text(json.dumps({"commit": verified}))
    stage = tmp_path / "stage"
    stage.mkdir()

    commit = cr.render(
        stage,
        record=record,
        mirror=origin / ".git",
        chezmoi=str(write_exec(tmp_path / "chezmoi", CHEZMOI_STUB)),
        config="/dev/null",
    )

    assert commit == verified
    assert (stage / "out" / "settings.base.json").read_text() == '{"tag": "verified"}'
    assert sorted(p.name for p in (stage / "out").iterdir()) == sorted(cr.OUTPUTS)


def _link_render(stage: Path) -> None:
    target = stage.parent / "elsewhere.json"
    target.write_text((stage / "out" / "settings.base.json").read_text())
    (stage / "out" / "settings.base.json").unlink()
    (stage / "out" / "settings.base.json").symlink_to(target)


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        (_link_render, "cannot open staged settings.base.json"),
        (
            lambda stage: (stage / "out" / "extra").write_text("x"),
            "staged output holds",
        ),
        (
            lambda stage: (stage / "out" / "commit").write_text(OTHER + "\n"),
            "is not the verified",
        ),
        (
            lambda stage: (stage / "out" / "settings.base.json").write_text(
                json.dumps({"permissions": {"deny": ["Bash(sudo:*)"]}, "autoMode": {}})
            ),
            "the render lacks",
        ),
        (
            lambda stage: (stage / "out" / "settings.base.json").write_text(
                json.dumps(
                    {
                        "permissions": {"deny": []},
                        "autoMode": {k: [] for k in cr.AUTO_MODE_LISTS}
                        | {"classifyAllShell": True},
                    }
                )
            ),
            "permissions.deny is empty",
        ),
    ],
    ids=["link", "extra-file", "foreign-commit", "no-auto-mode", "empty-deny"],
)
def test_a_tampered_or_incomplete_stage_publishes_nothing(host, tamper, reason):
    stage, paths = host
    tamper(stage)

    with pytest.raises(cr.Refused, match=reason):
        cr.publish(stage, **paths)

    assert not (paths["publish_dir"] / "current").exists()


def test_a_refused_publish_keeps_the_last_version(host, tmp_path):
    stage, paths = host
    cr.publish(stage, **paths)
    before = _published(paths, "triage.json")
    paths["record"].write_text(json.dumps({"commit": OTHER}))
    bad = _stage(tmp_path / "bad", OTHER, render="{}")

    with pytest.raises(cr.Refused):
        cr.publish(bad, **paths)

    assert _published(paths, "triage.json") == before
    assert _published(paths, "source.json") == {"commit": COMMIT}


def test_a_session_user_can_read_what_a_umask_0077_publish_writes(host):
    """The unit's UMask=0077 reaches the root publish; other session users must still read."""
    stage, paths = host
    previous = os.umask(0o077)
    try:
        cr.publish(stage, **paths)
    finally:
        os.umask(previous)

    root = paths["publish_dir"]
    version = (root / "current").resolve()
    for d in (root, root / "versions", version):
        assert d.stat().st_mode & 0o777 == 0o755, d
    for f in (root / "lock", *version.iterdir()):
        assert f.stat().st_mode & 0o777 == 0o644, f


def test_an_unchanged_render_publishes_no_new_version(host):
    stage, paths = host
    cr.publish(stage, **paths)

    assert cr.publish(stage, **paths) == f"unchanged at {COMMIT[:12]}"
    assert len(list((paths["publish_dir"] / "versions").iterdir())) == 1


def test_a_session_holding_the_config_defers_the_publish_and_keeps_its_file(
    host, tmp_path
):
    """Spec line 361: no swap and no prune while a session runs."""
    stage, paths = host
    cr.publish(stage, **paths)
    held = claude_config_lock.hold_settings(
        str(paths["publish_dir"] / "current" / "triage.json")
    )
    paths["record"].write_text(json.dumps({"commit": OTHER}))
    newer = _stage(tmp_path / "newer", OTHER)

    assert cr.publish(newer, **paths) == "deferred: a session holds the config"

    assert Path(held).is_file()
    assert _published(paths, "source.json") == {"commit": COMMIT}


def test_an_overlay_may_not_set_auto_mode(host):
    stage, paths = host
    (paths["overlay_dir"] / "triage.json").write_text(
        json.dumps({"autoMode": {"allow": ["*"]}})
    )

    with pytest.raises(cr.Refused, match="may not carry autoMode"):
        cr.publish(stage, **paths)
