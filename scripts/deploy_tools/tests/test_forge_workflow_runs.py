import json

import pytest

from deploy_tools import forge
from ci_verdict import WorkflowRun


def _github(runs: list) -> forge.GitHub:
    def transport(method, url, headers):
        body = {"total_count": len(runs), "workflow_runs": runs}
        return forge.Response(200, body=json.dumps(body).encode())

    return forge.GitHub(transport, lambda: "token")


def test_workflow_runs_are_parsed_where_forge_reads_them():
    raw = {"id": 8, "status": "in_progress", "head_sha": "a" * 40}
    assert _github([raw]).runs("promote.yml", "in_progress") == [WorkflowRun.parse(raw)]


def test_a_workflow_run_that_is_not_an_object_is_a_forge_error():
    with pytest.raises(forge.ForgeError, match=r"promote\.yml runs: a workflow run"):
        _github([8]).runs("promote.yml", "in_progress")
