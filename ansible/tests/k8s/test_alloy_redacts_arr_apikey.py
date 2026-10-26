"""The Alloy config must redact Prowlarr's `apikey=` out of the *arr Torznab error lines (#6276).

Sonarr and Radarr log every failed Torznab call with its full request URL, and that URL embeds
Prowlarr's API key as `apikey=<32 hex>`. Alloy ships those lines to `loki-homelab`, which the LAN
reaches with no Authelia in front of it. The redaction has two ways to be wrong, and each has a
test below:

- the key survives, and anyone on the LAN can read Prowlarr's key from the 744h store;
- the whole record goes with it, and the indexer name and search type a Torznab failure is
  diagnosed by are lost.

The stages are READ OUT of the rendered config rather than restated here. Each `stage.match`
block applies to a container when its selector does, and each `stage.replace` inside one
substitutes only its capture group, the way Alloy does. Alloy compiles the expression as RE2;
Python's `re` stands in, which is equivalent for a character class, a bounded repeat and one
capture group.
"""

import re

import pytest

from _k8s_render import rendered_docs

# The real key's shape (32 hex chars), with no entropy for gitleaks to read as a credential.
KEY = "f" * 32


def _torznab_line(app: str, key: str = KEY) -> str:
    search = "tvsearch" if app == "sonarr" else "movie"
    return (
        "[Warn] Torznab: Tokyo Toshokan (Prowlarr) server is currently unavailable. "
        f"http://prowlarr:9696/21/api?t={search}&cat=5000&extended=1&apikey={key}"
        "&offset=0&limit=100"
    )


# `stage.match { selector = "…"`, with the River string's `\"` escapes left in the capture.
_MATCH_BLOCK = re.compile(r'stage\.match\s*\{\s*selector\s*=\s*"((?:[^"\\]|\\.)*)"')
_REPLACE_STAGE = re.compile(
    r'stage\.replace\s*\{\s*expression\s*=\s*"((?:[^"\\]|\\.)*)"\s*\n\s*replace\s*=\s*"((?:[^"\\]|\\.)*)"'
)
_CONTAINER_MATCHER = re.compile(r'container\s*(=~|=)\s*"([^"]*)"')


@pytest.fixture(scope="module")
def alloy_config() -> str:
    for role, _tpl, doc in rendered_docs():
        if role == "loki-homelab" and doc.get("kind") == "ConfigMap":
            return doc["data"]["config.alloy"]
    raise AssertionError(
        "loki-homelab renders no ConfigMap carrying a config.alloy key"
    )


def _selector_applies(selector: str, container: str) -> bool:
    m = _CONTAINER_MATCHER.search(selector.replace('\\"', '"'))
    if not m:
        return False
    op, value = m.groups()
    # LogQL's `=~` is fully anchored.
    return (
        re.fullmatch(value, container) is not None if op == "=~" else value == container
    )


def _replace_stages_for(
    alloy_config: str, container: str
) -> list[tuple[re.Pattern, str]]:
    """Every (expression, replacement) a line from `container` passes through, in config order."""
    starts = list(_MATCH_BLOCK.finditer(alloy_config))
    stages = []
    for i, block in enumerate(starts):
        if not _selector_applies(block.group(1), container):
            continue
        end = starts[i + 1].start() if i + 1 < len(starts) else len(alloy_config)
        for stage in _REPLACE_STAGE.finditer(alloy_config, block.end(), end):
            stages.append(
                (re.compile(stage.group(1).replace('\\"', '"')), stage.group(2))
            )
    return stages


def _ship(alloy_config: str, container: str, line: str) -> str:
    """The line as Loki stores it: each stage's capture group replaced, the rest kept."""
    stages = _replace_stages_for(alloy_config, container)
    assert stages, f"no stage.replace applies to container {container!r}"
    for pattern, replacement in stages:
        assert pattern.groups == 1, (
            f"{pattern.pattern!r} must have exactly one capture group; Alloy replaces only it"
        )
        line = pattern.sub(
            lambda m, replacement=replacement: (
                m.group(0)[: m.start(1) - m.start(0)]
                + replacement
                + m.group(0)[m.end(1) - m.start(0) :]
            ),
            line,
        )
    return line


@pytest.mark.parametrize("app", ["sonarr", "radarr"])
def test_the_torznab_apikey_is_redacted(alloy_config: str, app: str) -> None:
    """The Verify-by: no `apikey=[0-9a-f]{32}` line reaches Loki after a Torznab failure."""
    out = _ship(alloy_config, app, _torznab_line(app))
    assert KEY not in out
    assert re.search(r"apikey=[0-9a-f]{32}", out) is None
    assert "apikey=<redacted>" in out


@pytest.mark.parametrize("app", ["prowlarr", "bazarr"])
def test_the_apikey_is_redacted_for_prowlarr_and_bazarr(
    alloy_config: str, app: str
) -> None:
    out = _ship(alloy_config, app, _torznab_line(app))
    assert KEY not in out


def test_an_upper_case_apikey_is_redacted(alloy_config: str) -> None:
    key = "F" * 32
    out = _ship(alloy_config, "sonarr", _torznab_line("sonarr", key))
    assert key not in out


def test_the_rest_of_the_torznab_record_is_kept(alloy_config: str) -> None:
    """Only the capture group goes: the indexer and the search type stay findable."""
    out = _ship(alloy_config, "sonarr", _torznab_line("sonarr"))
    assert KEY not in out
    assert "Tokyo Toshokan (Prowlarr)" in out
    assert "t=tvsearch" in out
    assert "&offset=0&limit=100" in out
