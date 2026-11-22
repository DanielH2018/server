import json

import pytest

from deploy_tools import forge


def _github(runs: list) -> forge.GitHub:
    def transport(method, url, headers):
        body = {"total_count": len(runs), "workflow_runs": runs}
        return forge.Response(200, body=json.dumps(body).encode())

    return forge.GitHub(transport, lambda: "token")


def test_a_workflow_run_that_is_not_an_object_is_a_forge_error():
    with pytest.raises(forge.ForgeError, match=r"promote\.yml runs: a workflow run"):
        _github([8]).runs("promote.yml", "in_progress")
