"""Every GitHub Actions job runs on a versioned runner image.

`runs-on: ubuntu-latest` floated in every workflow until #2152. The `language: system` prek
hooks run whatever shellcheck, Go and systemd-analyze the image preinstalls, and GitHub rolls
the alias to a new release with no commit in this repo — so a hook could start failing, or
start passing for a different reason, between two runs of the same SHA. A versioned label
(`ubuntu-24.04`) moves only through Renovate: the built-in github-actions manager reads it via
the github-runners datasource, and the bump PR runs the hooks on the new image before merge.
The pins share one depName there, so one PR moves every workflow; nothing here holds them
to one image, since a job may one day need an arm or larger runner.

Run: uv run pytest ansible/tests/repo/test_workflow_runners_are_pinned.py
"""

import re

from _helpers import REPO

WORKFLOWS = REPO / ".github" / "workflows"

# The census names its members: a glob that matched nothing would let `all()` pass over an
# empty set, and a renamed workflow would drop out of the check without a failure.
KNOWN_WORKFLOWS = frozenset({"ci.yml", "image-smoke.yml", "renovate-config-canary.yml"})

_RUNS_ON = re.compile(r"^\s*runs-on:\s*(.+?)\s*$", re.MULTILINE)
_VERSIONED = re.compile(r"^[a-z]+-\d+(\.\d+)?(-[a-z]+)?$")


def floating_runners(text: str) -> list[str]:
    """Every `runs-on` label in a workflow that is not a versioned image."""
    return [label for label in _RUNS_ON.findall(text) if not _VERSIONED.match(label)]


def _workflow_runners() -> dict[str, list[str]]:
    return {
        wf.name: _RUNS_ON.findall(wf.read_text())
        for wf in sorted(WORKFLOWS.glob("*.yml"))
    }


def test_a_versioned_label_is_clean():
    assert floating_runners("jobs:\n  a:\n    runs-on: ubuntu-24.04\n") == []


def test_a_latest_alias_is_flagged():
    text = "jobs:\n  a:\n    runs-on: ubuntu-latest\n  b:\n    runs-on: ${{ env.RUNNER }}\n"
    assert floating_runners(text) == ["ubuntu-latest", "${{ env.RUNNER }}"]


def test_every_workflow_job_runs_on_a_versioned_image():
    runners = _workflow_runners()
    assert KNOWN_WORKFLOWS <= runners.keys(), (
        f"workflow census lost {sorted(KNOWN_WORKFLOWS - runners.keys())}; the glob no longer "
        f"reaches them, so nothing below is checking them"
    )
    floating = {
        name: [label for label in labels if not _VERSIONED.match(label)]
        for name, labels in runners.items()
    }
    floating = {name: labels for name, labels in floating.items() if labels}
    assert not floating, (
        f"floating runner labels {floating} — pin a versioned image (`ubuntu-24.04`) so the "
        f"image moves through a Renovate PR rather than under a SHA already merged"
    )
