"""`role_defaults`: one parse of a role's `defaults/main.yml` into `RoleDefaults`."""

from pathlib import Path

from deploy_tools import bundle_census
from lib.k8s_roles import role_defaults


def _role(roles: Path, tag: str, body: str) -> Path:
    defaults = roles / tag / "defaults"
    defaults.mkdir(parents=True, exist_ok=True)
    path = defaults / "main.yml"
    path.write_text(body)
    return path


def test_a_tags_defaults_are_loaded_once_however_often_they_are_read(tmp_path):
    path = _role(
        tmp_path,
        "sonarr",
        "flux_suspend: true\nk8s_autodeploy_snapshot_pvcs: [b, a]\n"
        "flux_prepull_timeout: 600s\n",
    )
    first, _ = role_defaults(["sonarr"], tmp_path)
    path.write_text("flux_suspend: 'not read again'\n")
    second, problems = role_defaults(["sonarr"], tmp_path)
    assert problems == []
    assert second["sonarr"] is first["sonarr"]
    assert first["sonarr"].suspend is True
    assert first["sonarr"].snapshot_pvcs == ("a", "b")
    assert bundle_census.snapshot_tags(["sonarr"], tmp_path) == ({"sonarr"}, [])
    assert bundle_census.prepull_tags(["sonarr"], tmp_path) == ({"sonarr"}, [])


def test_a_wrong_typed_field_fails_every_reader_of_the_tag(tmp_path):
    _role(tmp_path, "sonarr", "flux_prune: 'no'\nk8s_autodeploy_snapshot_pvcs: [a]\n")
    problem = "sonarr's flux_prune is 'no', not a literal bool"
    assert role_defaults(["sonarr"], tmp_path) == ({}, [problem])
    assert bundle_census.snapshot_tags(["sonarr"], tmp_path) == (frozenset(), [problem])
