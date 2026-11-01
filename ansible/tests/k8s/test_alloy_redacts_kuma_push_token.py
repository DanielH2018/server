"""The Alloy config must redact Uptime Kuma push tokens out of the pod lines it ships (#6749).

A push URL carries its token as a path segment, `/api/push/<token>`. Over the 7 days to
2026-10-31, loki-homelab held such lines from three `homelab` containers: access-log-rotate's
copy of Traefik's access log, cloudflare-ddns's failed-ping error, and crowdsec-agent. Anyone on
the LAN reads loki-homelab without Authelia, for 744h.

The stages are READ OUT of the rendered `loki.process "pods"` block rather than restated here,
so the fix may scope its stage with any `stage.match` selector on these labels, or none. Each
`stage.replace` substitutes only its capture groups, the way Alloy does; Python's `re` stands in
for RE2, which is equivalent for a character class, a bounded repeat and a capture group.
"""

import re

import pytest

from _k8s_render import rendered_docs

# The token's shape the Verify-by query matches, with no entropy for gitleaks to read.
TOKEN = "f" * 32

LINES = {
    "access-log-rotate": (
        '{"ClientHost":"10.42.0.17","DownstreamStatus":404,"Duration":1200000000,'
        f'"RequestMethod":"GET","RequestPath":"/api/push/{TOKEN}?status=up&msg=OK&ping=",'
        '"RouterName":"uptime-kuma@kubernetescrd"}'
    ),
    "cloudflare-ddns": (
        "Failed to send HTTP(S) request to Uptime Kuma: "
        f"GET http://uptime-kuma:3001/api/push/{TOKEN}?msg=OK&status=up&ping="
    ),
    "crowdsec-agent": (
        'time="2026-10-30T11:02:41Z" level=info msg="Ip 10.42.0.17 performed '
        f'GET /api/push/{TOKEN}?status=up&msg=OK&ping="'
    ),
}

_STRING = r'"(?:[^"\\]|\\.)*"'
_MATCH_SELECTOR = re.compile(rf"stage\.match\s*\{{\s*selector\s*=\s*({_STRING})")
_REPLACE_STAGE = re.compile(
    rf"stage\.replace\s*\{{\s*expression\s*=\s*({_STRING})\s*\n\s*replace\s*=\s*({_STRING})"
)
_LABEL_MATCHER = re.compile(r'(\w+)\s*(=~|!~|!=|=)\s*"([^"]*)"')
_LINE_FILTER = re.compile(r'(\|=|\|~|!=|!~)\s*"([^"]*)"')


def _unquote(river: str) -> str:
    return river[1:-1].replace('\\"', '"').replace("\\\\", "\\")


def _block_end(text: str, open_brace: int) -> int:
    """Index just past the `}` closing the `{` at `open_brace`, skipping string contents."""
    depth, i = 0, open_brace
    while i < len(text):
        c = text[i]
        if c == '"':
            string = re.compile(_STRING).match(text, i)
            assert string, f"unterminated string at offset {i} in config.alloy"
            i = string.end()
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AssertionError("unbalanced braces in config.alloy")


@pytest.fixture(scope="module")
def pods_block() -> str:
    for role, _tpl, doc in rendered_docs():
        if role == "loki-homelab" and "config.alloy" in (doc.get("data") or {}):
            config = doc["data"]["config.alloy"]
            start = config.index('loki.process "pods"')
            return config[start : _block_end(config, config.index("{", start))]
    raise AssertionError(
        "loki-homelab renders no ConfigMap carrying a config.alloy key"
    )


def _selector_applies(selector: str, labels: dict[str, str], line: str) -> bool:
    stream, _, pipeline = selector.partition("}")
    for name, op, value in _LABEL_MATCHER.findall(stream):
        have = labels.get(name, "")
        hit = re.fullmatch(value, have) is not None if "~" in op else value == have
        if hit != (op in ("=", "=~")):
            return False
    for op, value in _LINE_FILTER.findall(pipeline):
        hit = re.search(value, line) is not None if "~" in op else value in line
        if hit != op.startswith("|"):
            return False
    return True


def _ship(block: str, labels: dict[str, str], line: str) -> str:
    """The line as Loki stores it: every applicable stage.replace's capture groups replaced."""
    matches = [
        (
            m.start(),
            _block_end(block, block.index("{", m.start())),
            _unquote(m.group(1)),
        )
        for m in _MATCH_SELECTOR.finditer(block)
    ]
    for stage in _REPLACE_STAGE.finditer(block):
        enclosing = [s for s in matches if s[0] < stage.start() < s[1]]
        if not all(_selector_applies(sel, labels, line) for _, _, sel in enclosing):
            continue
        pattern = re.compile(_unquote(stage.group(1)))
        replacement = _unquote(stage.group(2))
        if not pattern.groups:
            line = pattern.sub(replacement, line)
            continue

        def _sub(m: re.Match, replacement: str = replacement) -> str:
            out, pos = [], m.start(0)
            for g in range(1, m.re.groups + 1):
                if m.start(g) < 0:
                    continue
                out += [m.string[pos : m.start(g)], replacement]
                pos = m.end(g)
            return "".join(out) + m.string[pos : m.end(0)]

        line = pattern.sub(_sub, line)
    return line


def _labels(container: str) -> dict[str, str]:
    return {"container": container, "namespace": "homelab", "job": "k8s"}


@pytest.mark.parametrize("container", sorted(LINES))
def test_the_push_token_is_redacted(pods_block: str, container: str) -> None:
    """The Verify-by: no new loki-homelab line matches `api/push/[0-9a-f]{20,}`."""
    out = _ship(pods_block, _labels(container), LINES[container])
    assert TOKEN not in out
    assert re.search(r"api/push/[0-9a-f]{20,}", out) is None


def test_the_rest_of_the_push_record_is_kept(pods_block: str) -> None:
    """Only the token goes: the failing target and the ping's message stay findable."""
    out = _ship(pods_block, _labels("cloudflare-ddns"), LINES["cloudflare-ddns"])
    assert TOKEN not in out
    assert "Failed to send HTTP(S) request to Uptime Kuma" in out
    assert "http://uptime-kuma:3001/api/push/" in out
    assert "?msg=OK&status=up&ping=" in out
