"""The paths ADR 0030 retired are gone from the comments that reach a manifest or an image (#8405).

#8186 renamed `ansible/` without changing a rendered manifest, so every comment and docstring
under `k8s/` and `scripts/renderer/job_files/` kept its pre-rename path. A line may still name
`ansible/` when it is about ansible-core, or when it names a path inside a historical commit
(`git show <sha>:ansible/...`), where the old path is the correct one.

Run: uv run pytest tests/repo/test_pre_rename_paths_census.py
"""

import re

from _helpers import REPO
from _row_table import tracked

RENDERED_TREES = ("k8s", "scripts/renderer/job_files")
OBS_API = "scripts/diagnostics/probe_lib/obs_api.py"
AT_A_COMMIT = re.compile(r"\b[0-9a-f]{7,40}:ansible/")
BARE_ROLES_K8S = re.compile(r"(?<![\w-])roles/k8s/")
MUST_SCAN = frozenset(
    {"k8s/roles/homelab-mcp/files/app.py", "k8s/templates/networkpolicy.yml.j2"}
)


def _corpus() -> list[str]:
    paths = tracked(*RENDERED_TREES)
    assert MUST_SCAN <= set(paths), (
        f"the census no longer reads {sorted(MUST_SCAN - set(paths))}"
    )
    return paths


def _lines(paths: list[str]) -> list[tuple[str, int, str]]:
    out = []
    for rel in paths:
        try:
            text = (REPO / rel).read_text()
        except UnicodeDecodeError:
            continue
        out.extend((rel, n, line) for n, line in enumerate(text.splitlines(), 1))
    return out


def _ansible_path_offenders(paths: list[str]) -> list[str]:
    return [
        f"{rel}:{n}: {line.strip()}"
        for rel, n, line in _lines(paths)
        if "ansible/" in line
        and "ansible-core" not in line
        and not AT_A_COMMIT.search(line)
    ]


def test_ansible_slash_under_k8s_and_job_files_names_only_ansible_core():
    offenders = _ansible_path_offenders(_corpus())
    assert not offenders, (
        f"{len(offenders)} lines still cite a pre-ADR-0030 ansible/ path:\n"
        + "\n".join(offenders[:40])
    )


def test_no_comment_under_k8s_or_job_files_cites_the_bare_roles_k8s_path():
    offenders = [
        f"{rel}:{n}: {line.strip()}"
        for rel, n, line in _lines(_corpus())
        if BARE_ROLES_K8S.search(line)
    ]
    assert not offenders, (
        f"{len(offenders)} lines cite roles/k8s/<role>, which ADR 0030 renamed k8s/roles/<role>:\n"
        + "\n".join(offenders[:40])
    )


def test_obs_api_docstring_cites_the_adr_0030_paths():
    text = (REPO / OBS_API).read_text()
    stale = sorted(
        {
            m.group(0)
            for m in re.finditer(r"ansible/[\w./-]+|(?<![\w-])roles/k8s/[\w./-]+", text)
        }
    )
    assert not stale, f"{OBS_API} still cites pre-rename paths: {stale}"
