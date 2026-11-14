"""claude_guardrail_ack.run over a tmp_path tree, with dpkg, chezmoi and Discord as fakes."""

import hashlib
import json

import claude_guardrail_ack as ack
import pytest

TOKEN = "ghp_" + "A" * 36


@pytest.fixture
def tree(tmp_path):
    """A home with settings.json and a hook, a package-owned file, and the run's config."""
    home = tmp_path / "home"
    (home / "hooks" / "__pycache__").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"model": "opus", "hooks": {}}))
    (home / "hooks" / "guard.sh").write_text("#!/bin/sh\nexit 0\n")
    package_file = tmp_path / "approval-paths"
    package_file.write_text("scripts/lib/\n")
    config = {
        "paths": [str(home / "settings.json"), str(home / "hooks"), str(package_file)],
        "homes": [{"home": str(home), "user": "ubuntu", "chezmoi": "chezmoi"}],
        "quiet_settings_keys": ["model", "effortLevel"],
        "dotfiles_repo": "unused",
        "dotfiles_clone": "unused",
    }
    return home, package_file, config


PUBLISHED = {"homelab-daniel-box": ["0.7465+g203fe8f7bc22", "0.7460+g840813a2285a"]}


def _run(tmp_path, config, *, rendered=None, owners=None, debs=None, deliver=True):
    """One pass; returns (ok, verdict, the posts made)."""
    posts = []

    def post(content):
        posts.append(content)
        return deliver

    tools = ack.Boundaries(
        owner=lambda path: (owners or {}).get(path),
        published=lambda package: PUBLISHED.get(package, []),
        published_md5s=lambda package, version: (debs or {}).get((package, version)),
        render=lambda user, chezmoi, source, target: (rendered or {}).get(target),
        sync=lambda repo, clone: "b0b8bf3f",
    )
    ok, line = ack.run(config, tmp_path / "state", "daniel-box", post, tools)
    return ok, line, posts


def test_the_first_run_baselines_and_posts_nothing(tmp_path, tree):
    _, _, config = tree
    ok, line, posts = _run(tmp_path, config)
    assert (ok, posts) == (True, [])
    assert line == "baselined 3 files; nothing posted"


def test_a_hand_edit_to_a_hook_is_posted_with_its_secret_masked(tmp_path, tree):
    home, _, config = tree
    _run(tmp_path, config)
    bare = "Zx" * 20
    (home / "hooks" / "guard.sh").write_text(
        f"#!/bin/sh\nexport GH_TOKEN={TOKEN}\ncurl -H 'X-Id: {bare}' x\nexit 0\n"
    )
    ok, line, [post] = _run(tmp_path, config)
    assert ok and "1 posted" in line
    assert f"`{home}/hooks/guard.sh` changed" in post
    assert "+export GH_TOKEN= <masked>" in post
    assert "+curl -H 'X-Id: <masked>' x" in post
    assert TOKEN not in post and bare not in post


def test_a_change_that_matches_the_dotfiles_main_render_is_not_posted(tmp_path, tree):
    home, _, config = tree
    _run(tmp_path, config)
    new = b"#!/bin/sh\nexit 1\n"
    (home / "hooks" / "guard.sh").write_bytes(new)
    ok, line, posts = _run(
        tmp_path, config, rendered={str(home / "hooks" / "guard.sh"): new}
    )
    assert (ok, posts) == (True, [])
    assert "1 explained, 0 posted" in line


def _owned(path, version):
    """The installed package's record for `path` as it reads now."""
    digest = hashlib.md5(path.read_bytes()).hexdigest()
    return {str(path): ack.Owner("homelab-daniel-box", version, digest)}


def _deb(path, version, content):
    """A published .deb of `version` whose md5sums record `content` for `path`."""
    digest = hashlib.md5(content).hexdigest()
    return {("homelab-daniel-box", version): {str(path): digest}}


def test_a_package_file_matching_its_md5_is_not_posted_and_a_drifted_one_is(
    tmp_path, tree
):
    _, package_file, config = tree
    _run(tmp_path, config)
    package_file.write_text("scripts/lib/\nscripts/deploy_tools/\n")
    installed = _owned(package_file, "0.7465+g203fe8f7bc22")
    deb = _deb(package_file, "0.7465+g203fe8f7bc22", package_file.read_bytes())
    assert _run(tmp_path, config, owners=installed, debs=deb)[2] == []
    package_file.write_text("scripts/lib/\n")
    [post] = _run(tmp_path, config, owners=installed, debs=deb)[2]
    assert f"`{package_file}` changed\n" in post


def test_a_hand_installed_package_change_is_posted_with_both_versions(tmp_path, tree):
    _, package_file, config = tree
    _run(tmp_path, config)
    package_file.write_text("scripts/lib/\nscripts/deploy_tools/\n")
    hand = _owned(package_file, "0.7466+g0123456789ab")
    ok, line, [post] = _run(tmp_path, config, owners=hand)
    assert ok and "0 explained, 1 posted" in line
    assert (
        f"`{package_file}` changed (homelab-daniel-box 0.7466+g0123456789ab is installed, "
        "and no apt source serves it; the newest published version is "
        "0.7465+g203fe8f7bc22)"
    ) in post
    assert "+scripts/deploy_tools/" in post


def test_a_hand_build_reusing_a_published_version_is_posted(tmp_path, tree):
    _, package_file, config = tree
    _run(tmp_path, config)
    package_file.write_text("scripts/lib/\nscripts/deploy_tools/\n")
    reused = _owned(package_file, "0.7465+g203fe8f7bc22")
    deb = _deb(package_file, "0.7465+g203fe8f7bc22", b"scripts/lib/\n")
    ok, line, [post] = _run(tmp_path, config, owners=reused, debs=deb)
    assert ok and "0 explained, 1 posted" in line
    assert (
        f"`{package_file}` changed (homelab-daniel-box 0.7465+g203fe8f7bc22 is installed, "
        "and the file differs from the one in the published package of that version)"
    ) in post


def test_published_versions_skips_one_only_dpkg_status_lists():
    policy = (
        "homelab-daniel-box:\n"
        "  Installed: 0.7466+g0123456789ab\n"
        "  Candidate: 0.7466+g0123456789ab\n"
        "  Version table:\n"
        " *** 0.7466+g0123456789ab 100\n"
        "        100 /var/lib/dpkg/status\n"
        "     0.7465+g203fe8f7bc22 500\n"
        "        500 http://10.0.0.215:8791 homelab/main amd64 Packages\n"
        "     0.7460+g840813a2285a 500\n"
        "        500 http://10.0.0.215:8791 homelab/main amd64 Packages\n"
    )
    assert ack.published_versions(policy) == [
        "0.7465+g203fe8f7bc22",
        "0.7460+g840813a2285a",
    ]


def test_claude_codes_own_model_write_is_quiet_and_a_permissions_change_is_posted(
    tmp_path, tree
):
    home, _, config = tree
    settings = home / "settings.json"
    _run(tmp_path, config)
    settings.write_text(json.dumps({"model": "sonnet", "hooks": {}}))
    assert _run(tmp_path, config)[2] == []
    settings.write_text(
        json.dumps({"model": "sonnet", "hooks": {}, "permissions": {"allow": ["Bash"]}})
    )
    [post] = _run(tmp_path, config)[2]
    assert '"permissions"' in post


def test_a_failed_post_is_retried_on_the_next_run(tmp_path, tree):
    home, _, config = tree
    _run(tmp_path, config)
    (home / "hooks" / "new.sh").write_text("#!/bin/sh\n")
    ok, line, _ = _run(tmp_path, config, deliver=False)
    assert not ok and "retried next run" in line
    [post] = _run(tmp_path, config)[2]
    assert f"`{home}/hooks/new.sh` added" in post
    assert _run(tmp_path, config)[2] == []


def test_bytecode_under_a_hooks_directory_is_ignored(tmp_path, tree):
    home, _, config = tree
    _run(tmp_path, config)
    (home / "hooks" / "__pycache__" / "guard.cpython-312.pyc").write_bytes(b"\x00\x01")
    assert _run(tmp_path, config)[2] == []
