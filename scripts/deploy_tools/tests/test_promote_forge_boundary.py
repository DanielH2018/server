"""`promote.py` reaches GitHub and GHCR only through `forge.py`, and lists no tags.

Two properties of the boundary. The import census reads `promote.py`'s imports: no HTTP
library, and no `subprocess`, so it cannot start `gh` itself. The request count runs `decide`
against a registry holding 3,000 `sha-*` tags and counts what it asked ghcr.io for.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_forge_boundary.py
"""

import ast
from pathlib import Path

import forge
import promote
from _promote_fakes import World, digest_of

PROMOTE = Path(promote.__file__)

# The modules that would let promote.py make a forge call without the adapter. `subprocess`
# is here because `gh` and `cosign` are forge calls too; git goes through `lib.git`.
FORBIDDEN = ("urllib", "http", "requests", "httpx", "urllib3", "socket", "subprocess")

# The adapter itself: the one member the census must find, so a rename that leaves promote.py
# importing nothing cannot pass as "imports nothing forbidden".
ADAPTER = "forge"


def imported_modules(source: str) -> set[str]:
    """Every module name an `import` or `from ... import` in `source` names."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def forbidden_imports(source: str) -> set[str]:
    return {
        name for name in imported_modules(source) if name.split(".")[0] in FORBIDDEN
    }


def test_promote_imports_the_adapter_and_no_http_library_is_clean():
    source = PROMOTE.read_text(encoding="utf-8")
    assert ADAPTER in imported_modules(source)
    assert forbidden_imports(source) == set()


def test_a_module_importing_the_http_library_itself_is_flagged():
    source = "import forge\nimport urllib.request\n\nurllib.request.urlopen('https://ghcr.io')\n"
    assert forbidden_imports(source) == {"urllib.request"}


def test_a_module_starting_gh_itself_is_flagged():
    source = (
        "import forge\nfrom subprocess import run\n\nrun(['gh', 'variable', 'get'])\n"
    )
    assert forbidden_imports(source) == {"subprocess"}


# -- request count -------------------------------------------------------------------------

TAGS_PUBLISHED = 3000


def _world_with_3000_tags(tmp_path) -> World:
    world = World(tmp_path, size=6)
    for n in range(TAGS_PUBLISHED):
        world.transport.tags[f"sha-{n:040x}"] = digest_of(str(n))
    world.set_prod(world.commits[0])
    return world


def _registry_requests_within_budget(world: World, examined: list[str]) -> bool:
    """Exactly one token request, one `prod` fetch and one `HEAD` per examined candidate."""
    repository = f"/v2/{forge.ARTIFACT}/manifests"
    expected = [("GET", "/token"), ("GET", f"{repository}/prod")]
    expected += [("HEAD", f"{repository}/sha-{sha}") for sha in examined]
    return world.transport.registry_requests() == expected


def test_decide_makes_one_head_per_candidate_examined_is_clean(
    tmp_path, monkeypatch, capsys
):
    """The tip and the next commit are unpublished, so the walk examines three candidates."""
    world = _world_with_3000_tags(tmp_path)
    *_, c3, c4, tip = world.commits
    world.publish(c3)
    result = world.decide(monkeypatch, capsys, trigger=tip)
    assert result.outputs["target"] == c3
    assert len(world.transport.tags) > TAGS_PUBLISHED
    assert _registry_requests_within_budget(world, [tip, c4, c3])


class _ListingRegistry(forge.Registry):
    """A registry client that lists every tag before each `HEAD`, as `flux list` does."""

    def digest(self, reference):
        self._transport(
            "GET",
            f"https://{forge.REGISTRY}/v2/{self.repository}/tags/list",
            {"Authorization": f"Bearer {self._token()}"},
        )
        return super().digest(reference)


def test_a_client_that_lists_tags_is_flagged(tmp_path, monkeypatch, capsys):
    world = _world_with_3000_tags(tmp_path)
    tip = world.commits[-1]
    world.publish(tip)
    registry = _ListingRegistry(world.transport, "job-token")
    result = world.decide(monkeypatch, capsys, trigger=tip, registry=registry)
    assert result.outputs["target"] == tip
    assert not _registry_requests_within_budget(world, [tip])
