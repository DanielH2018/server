"""Each threshold has one home: the env-secret renders it, and `bridge/config*.py` only reads it.

Until #3659 `TRAEFIK_421_RPS` defaulted to "0.02" in both `templates/env-secret.yaml.j2` and
`bridge/config_service.py`, and 40 of the 133 keys both files named disagreed (`TARGETS_MIN` 1
against 2, `PROWLARR_INDEXER_MIN_DOWN_MIN` 10080 against 30). The pod read the template while the
suite read the Python, so a test could pass at a value nothing deployed. A read with no default is
required, and these two tests hold that shape in both directions.
"""

import ast
from pathlib import Path

from _bridge_env import bridge_env

BRIDGE = Path(__file__).resolve().parents[1] / "files" / "bridge"
PARSERS = {"_env", "_int", "_num"}


def _reads() -> list[tuple[str, str, bool]]:
    """(module, key, has a default) for every `_env`/`_int`/`_num` read of a literal key."""
    found: list[tuple[str, str, bool]] = []
    for path in sorted(BRIDGE.glob("config*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in PARSERS
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                found.append((path.name, str(node.args[0].value), len(node.args) > 1))
    return found


def test_no_rendered_key_carries_a_second_default_in_python():
    rendered = set(bridge_env())
    reads = _reads()
    # Named members, so a parser rename that empties `_reads()` fails here, not silently.
    assert ("config_service.py", "TRAEFIK_421_RPS", False) in reads
    assert ("config.py", "K8S_EXTENDED_RESOURCES", True) in reads
    doubled = sorted(
        (m, k) for m, k, has_default in reads if has_default and k in rendered
    )
    assert not doubled, (
        "these keys are rendered by templates/env-secret.yaml.j2, so a Python default is a "
        "second copy the pod never reads; drop it: %s" % doubled
    )


def test_every_required_read_is_a_key_the_env_secret_renders():
    # The other direction: a required read the template does not render is a key the pod never
    # gets, and main() would exit 2 on every start.
    rendered = set(bridge_env())
    missing = sorted(
        (m, k)
        for m, k, has_default in _reads()
        if not has_default and k not in rendered
    )
    assert not missing, missing
