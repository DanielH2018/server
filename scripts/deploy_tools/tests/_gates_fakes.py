"""A fake `lib.kubectl.Tools` for the `*_gates.py` tests, and the documents they share.

The runner takes a `Tools`, so a test answers every cluster read from canned JSON keyed on
the kubectl arguments after `--kubeconfig <path>` and nothing touches a live API server. The
identity read (`get nodes -o json`) is answered too, so the cluster check passes as `prod`
unless a test hands it a staging node list. Imported by bare name: pytest puts this
directory on `sys.path` and the tests import it the way `_land_fakes` is imported.
"""

import json
import subprocess
from pathlib import Path

from lib import kubectl

NODES_ARGS = ("get", "nodes", "-o", "json")


def node(name: str, ready: str = "True") -> dict:
    return {
        "metadata": {"name": name},
        "status": {"conditions": [{"type": "Ready", "status": ready}]},
    }


BOTH_READY = {"items": [node("daniel-box"), node("daniel-server")]}


def fake_tools(answers: dict[tuple, object]) -> kubectl.Tools:
    """A `Tools` whose kubectl answers each argument tuple in `answers` with its document.

    A read the table does not carry raises `KeyError`, so a gate that reaches for a resource
    the test did not plan for fails loudly instead of passing on an empty answer. The node
    list defaults to the production pair.
    """
    table = {NODES_ARGS: BOTH_READY, **answers}

    def run(argv, timeout):
        args = tuple(argv[argv.index("--kubeconfig") + 2 :])
        return subprocess.CompletedProcess(argv, 0, json.dumps(table[args]), "")

    return kubectl.Tools(
        run=run,
        find_tool=lambda name: "/usr/local/bin/kubectl",
        find_kubeconfig=lambda: Path("/tmp/kubeconfig"),
    )


def failing_read(tools: kubectl.Tools, resource: str) -> kubectl.Tools:
    """`tools` with every read naming `resource` failing as a Forbidden non-zero exit."""
    real_run = tools.run

    def run(argv, timeout):
        if resource in argv:
            return subprocess.CompletedProcess(argv, 1, "", "Forbidden")
        return real_run(argv, timeout)

    return kubectl.Tools(run, tools.find_tool, tools.find_kubeconfig)
