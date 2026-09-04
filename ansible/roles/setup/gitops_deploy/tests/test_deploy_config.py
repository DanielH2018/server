"""Parsing the deployer's config cannot fail; only running with a bad one can.

`load_config` used to be ~40 `int(C.get(...))` calls evaluated at module level, so a config
file with `HEALTH_TIMEOUT_S=5m` in it raised `ValueError: invalid literal for int() with base
10: '5m'` during IMPORT — before the process had a webhook, a log line or a heartbeat, and with
no key name anywhere in the traceback. The parse now records the error and `Config.validate()`
raises it from inside `main()`, where `entrypoint()` turns it into one line and a Discord post.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deploy_config.py
"""

import dataclasses

import pytest

import deploy_io


def test_an_empty_environment_is_a_usable_default_config():
    cfg = deploy_io.load_config({})
    cfg.validate()
    assert cfg.repo == "" and cfg.branch == "master"
    assert cfg.health_timeout_s == 300 and cfg.k8s_deploy_timeout_s == 900


def test_the_config_is_frozen():
    """Nothing rebinds a value mid-tick: the CI gate and the denylist disarm by deriving a new
    value, never by writing one back."""
    cfg = deploy_io.load_config({})
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.repo = "/tmp/elsewhere"


def test_values_are_read_off_the_environment():
    cfg = deploy_io.load_config(
        {
            "REPO_DIR": "/home/ubuntu/server",
            "BRANCH": "main",
            "REQUIRE_CI": "true",
            "CI_CONTEXTS": "lint, test ",
            "K8S_DEPLOY_TIMEOUT_S": "1200",
        }
    )
    cfg.validate()
    assert cfg.repo == "/home/ubuntu/server" and cfg.branch == "main"
    assert cfg.require_ci is True
    assert cfg.ci_contexts == frozenset({"lint", "test"})
    assert cfg.k8s_deploy_timeout_s == 1200


def test_a_boolean_is_true_only_for_the_literal_word():
    """`true` and nothing else, case-insensitively — the same test the module made inline."""
    assert deploy_io.load_config({"STAGING_GATE": "TRUE"}).staging_gate is True
    for raw in ("false", "1", "yes", "", "True "):
        assert deploy_io.load_config({"STAGING_GATE": raw}).staging_gate is False, raw


# ── a malformed value ─────────────────────────────────────────────────────────────────────
def test_a_malformed_number_does_not_raise_at_parse_time():
    """The property that keeps a half-written config out of the import path."""
    cfg = deploy_io.load_config({"HEALTH_TIMEOUT_S": "5m"})
    assert cfg.health_timeout_s == 300, (
        "the field keeps its default while the error is held"
    )
    assert cfg.errors


def test_validate_raises_one_line_naming_the_key_and_what_it_held():
    cfg = deploy_io.load_config({"HEALTH_TIMEOUT_S": "5m"})
    with pytest.raises(deploy_io.ConfigError) as excinfo:
        cfg.validate()
    message = str(excinfo.value)
    assert "\n" not in message, (
        f"a diagnosable failure is one line, not a block: {message}"
    )
    assert "HEALTH_TIMEOUT_S" in message and "5m" in message


def test_every_malformed_key_is_named_not_just_the_first():
    """A truncated config.env usually damages more than one key; naming one at a time turns a
    single re-render into several."""
    cfg = deploy_io.load_config({"HEALTH_TIMEOUT_S": "5m", "RUN_BUDGET_S": "lots"})
    with pytest.raises(deploy_io.ConfigError) as excinfo:
        cfg.validate()
    assert "HEALTH_TIMEOUT_S" in str(excinfo.value)
    assert "RUN_BUDGET_S" in str(excinfo.value)


def test_a_good_config_validates_silently():
    """The accepting half of the pair above — without it the check could raise on everything."""
    deploy_io.load_config({"HEALTH_TIMEOUT_S": "600"}).validate()


# ── the file reader ───────────────────────────────────────────────────────────────────────
def test_an_absent_config_file_is_an_empty_mapping(tmp_path):
    """A missing file used to crash the import. It now leaves `repo` empty, and main() refuses
    to tick on that — the same page, raised from a function a test can call."""
    assert deploy_io.read_config_file(str(tmp_path / "absent.env")) == {}


def test_the_reader_parses_key_value_pairs(tmp_path):
    path = tmp_path / "deployer.env"
    path.write_text("REPO_DIR=/srv/repo\nBRANCH=master\n")
    assert deploy_io.read_config_file(str(path)) == {
        "REPO_DIR": "/srv/repo",
        "BRANCH": "master",
    }
