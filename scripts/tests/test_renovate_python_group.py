#!/usr/bin/env python3
"""Guard that one Renovate PR moves every Python pin together (#5479).

Three managers report the repo's Python as packageName `python`: the custom regex manager for
the CI setup action, the built-in pyenv manager for `.python-version`, and the built-in
dockerfile manager for `FROM python:` tags. Without a shared group each opened its own PR.
Renovate PR #5363 bumped only the CI pin to 3.15.0, so `test_python_version_pins_in_lockstep`
and `test_python_version_consistency.py` failed on it for good. Renovate rebased it onto every
master move: 44 red CI runs in about 46 hours.

Run: uv run pytest scripts/tests/test_renovate_python_group.py
"""

from _renovate import _PACKAGE_RULES, _resolve_setting

# Every site that reports the repo's Python to Renovate: (path, manager, datasource).
PYTHON_PIN_SITES = [
    (".github/actions/setup/action.yml", "custom.regex", "python-version"),
    (".python-version", "pyenv", "docker"),
    ("k8s/roles/ical-proxy/templates/Dockerfile.j2", "dockerfile", "docker"),
    ("k8s/roles/homelab-mcp/templates/Dockerfile.j2", "dockerfile", "docker"),
    # One role on the k8s_autodeploy denylist and one off it: the denylist rule and the plain
    # automerge path each claimed these pins before #8532.
    ("k8s/roles/monitor-bridge/defaults/main.yml", "custom.regex", "docker"),
    ("k8s/roles/sonarr/defaults/main.yml", "custom.regex", "docker"),
]

GROUP_PREFIX = "python (manual"


def _group(rules: list[dict], site: tuple[str, str, str], kind: str) -> str | None:
    path, manager, datasource = site
    return _resolve_setting(
        "groupName", "python", path, kind, datasource, rules, manager=manager
    )


def test_every_python_pin_site_falls_in_one_manual_group() -> None:
    for kind in ("minor", "major"):
        groups = {site: _group(_PACKAGE_RULES, site, kind) for site in PYTHON_PIN_SITES}
        assert len(set(groups.values())) == 1, f"{kind} bump splits: {groups}"
        (name,) = set(groups.values())
        assert name and name.startswith(GROUP_PREFIX), f"{kind} group is {name!r}"


def test_a_python_site_outside_the_rule_is_flagged() -> None:
    """The red proof: drop `.python-version` from the rule and that site leaves the group."""
    rules = [
        {
            **r,
            "matchFileNames": [
                f for f in r["matchFileNames"] if f != ".python-version"
            ],
        }
        if r.get("groupName", "").startswith(GROUP_PREFIX)
        else r
        for r in _PACKAGE_RULES
    ]
    assert _group(rules, PYTHON_PIN_SITES[1], "minor") is None


def test_the_manual_group_admits_no_patch_or_digest_bump() -> None:
    """Boundary guard: `matchUpdateTypes` keeps the k8s pins' digest automerge outside the group."""
    image = PYTHON_PIN_SITES[-1]
    assert _group(_PACKAGE_RULES, PYTHON_PIN_SITES[0], "patch") == "{{depName}}"
    assert not (_group(_PACKAGE_RULES, image, "digest") or "").startswith(GROUP_PREFIX)


def test_a_k8s_python_image_minor_bump_never_automerges() -> None:
    """Before #8532 sonarr's pin automerged a minor bump, so pods could run ahead of CI."""
    for site in PYTHON_PIN_SITES[-2:]:
        path, manager, datasource = site
        automerge = _resolve_setting(
            "automerge", "python", path, "minor", datasource, manager=manager
        )
        assert automerge is False, f"{path}: automerge resolves to {automerge!r}"
