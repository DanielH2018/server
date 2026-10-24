"""The Alloy `loki.process "host"` stage must label host lines by program and leave them intact.

The HostLogErrors rule groups host syslog by a `program` label, which this stage extracts from
the rsyslog tag (#6163). The stage has two ways to be wrong:

- it labels nothing, or labels the wrong field, and HostLogErrors fires with `program` empty;
- it rewrites the line, and probe.py's `SYSLOG_ALERT_LOGQL` and the alert-history dashboard,
  which parse the raw rsyslog shape, stop matching.

The expression is READ OUT of the rendered config rather than restated here. Alloy compiles it
as RE2; Python's `re` stands in, which is equivalent for an anchored pattern with `\\S`, `\\s`, a
negated class and one named group.
"""

import re

import pytest

from _k8s_render import rendered_docs

# Real line shapes from daniel-box and daniel-server syslog and auth.log, 2026-10-24.
LINES = {
    "2026-10-24T20:54:47.994980+00:00 daniel-server kernel: IPv4: martian source": "kernel",
    "2026-10-22T02:11:03.120000+00:00 daniel-box uv[1333]: Traceback (most recent call last):": "uv",
    "2026-10-24T03:00:01.000000+00:00 daniel-server postfix/qmgr[812]: 4F2A1: removed": "postfix/qmgr",
    "2026-10-22T18:17:15.401395+00:00 daniel-box docs-refresh: status=down docs-refresh: x": "docs-refresh",
    "2026-10-24T20:17:01.000000+00:00 daniel-box CRON[4021]: pam_unix(cron:session): opened": "CRON",
}

_HOST_BLOCK = re.compile(r'loki\.process "host" \{(.*?)\n\}', re.DOTALL)


@pytest.fixture(scope="module")
def alloy_config() -> str:
    for role, _tpl, doc in rendered_docs():
        if role == "loki-homelab" and doc.get("kind") == "ConfigMap":
            return doc["data"]["config.alloy"]
    raise AssertionError(
        "loki-homelab renders no ConfigMap carrying a config.alloy key"
    )


@pytest.fixture(scope="module")
def host_block(alloy_config: str) -> str:
    m = _HOST_BLOCK.search(alloy_config)
    assert m, 'no loki.process "host" block in the rendered Alloy config'
    return m.group(1)


def test_the_stage_extracts_the_rsyslog_tag(host_block: str) -> None:
    m = re.search(r"stage\.regex\s*\{\s*expression\s*=\s*`([^`]+)`", host_block)
    assert m, "no backtick stage.regex expression in the host block"
    pattern = re.compile(m.group(1))
    extracted = {}
    for line in LINES:
        match = pattern.match(line)
        extracted[line] = match["program"] if match else None
    assert extracted == LINES
    assert re.search(
        r"stage\.labels\s*\{\s*values\s*=\s*\{\s*program\s*=", host_block
    ), "the host block extracts `program` but no stage.labels promotes it to a label"


def test_the_stage_leaves_the_line_unchanged(host_block: str) -> None:
    """Only stage.regex and stage.labels may run here; every other stage can alter the line."""
    stages = set(re.findall(r"\bstage\.(\w+)\s*\{", host_block))
    assert stages == {"regex", "labels"}, sorted(stages)


def test_both_host_sources_pass_through_the_stage(alloy_config: str) -> None:
    for source in ("syslog", "authlog"):
        m = re.search(
            rf'loki\.source\.file "{source}" \{{[^}}]*forward_to\s*=\s*\[([^\]]*)\]',
            alloy_config,
        )
        assert m, f'no loki.source.file "{source}" with a forward_to'
        assert m.group(1).strip() == "loki.process.host.receiver", (source, m.group(1))
