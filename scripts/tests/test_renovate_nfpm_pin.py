#!/usr/bin/env python3
"""Guard the nfpm pin in CI's `host_packages` job (target slice H3, PR H3.3a).

The job pins nfpm by version and by sha256, and one custom manager moves both through the
github-release-attachments datasource. A manager that captured only the version would bump it
beside the old digest, and the job's `sha256sum -c` would fail every run. The grouping case in
`test_renovate_managers.py` cannot see either half: it resolves the `{{depName}}` group from the
`.github/workflows/**` packageRule, which matches with or without this manager.

Run: uv run pytest scripts/tests/test_renovate_nfpm_pin.py
"""

import re

from lib import yaml_fast

from _renovate import _CONFIG_MANAGERS, _REPO, _file_pattern_to_regex, _to_python_regex


CI_WORKFLOW = ".github/workflows/ci.yml"


def nfpm_pins(
    managers: list[dict], text: str
) -> list[tuple[str | None, str | None, str | None]]:
    """Each (datasource, currentValue, currentDigest) a goreleaser/nfpm manager reads from ci.yml.

    The digest is half the pin: a manager that matched only the version would move the version
    and leave the old digest, and the job's `sha256sum -c` would then fail every run.
    """
    pins = []
    for mgr in managers:
        if mgr.get("depNameTemplate") != "goreleaser/nfpm" or not any(
            _file_pattern_to_regex(fp).search(CI_WORKFLOW)
            for fp in mgr["managerFilePatterns"]
        ):
            continue
        for ms in mgr["matchStrings"]:
            for m in re.finditer(_to_python_regex(ms), text):
                groups = m.groupdict()
                pins.append(
                    (
                        mgr.get("datasourceTemplate"),
                        groups.get("currentValue"),
                        groups.get("currentDigest"),
                    )
                )
    return pins


def test_the_nfpm_manager_reads_the_jobs_version_and_digest_in_one_match() -> None:
    text = (_REPO / CI_WORKFLOW).read_text()
    env = yaml_fast.safe_load(text)["jobs"]["host_packages_build"]["env"]
    assert nfpm_pins(_CONFIG_MANAGERS, text) == [
        ("github-release-attachments", env["NFPM_VERSION"], env["NFPM_SHA256"])
    ]


def test_a_missing_or_version_only_nfpm_manager_is_flagged() -> None:
    """The reject halves: no manager at all, and one whose span drops the digest line."""
    text = (_REPO / CI_WORKFLOW).read_text()
    others = [
        m for m in _CONFIG_MANAGERS if m.get("depNameTemplate") != "goreleaser/nfpm"
    ]
    assert nfpm_pins(others, text) == []
    nfpm = next(
        m for m in _CONFIG_MANAGERS if m.get("depNameTemplate") == "goreleaser/nfpm"
    )
    version_only = {
        **nfpm,
        "matchStrings": [
            ms.split("[ \\t]*\\n[ \\t]+NFPM_SHA256")[0] for ms in nfpm["matchStrings"]
        ],
    }
    [(_, _, digest)] = nfpm_pins([version_only], text)
    assert digest is None
