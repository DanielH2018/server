"""The Alloy `host_audit` pipeline ships every keyed audit record and drops the unkeyed noise.

server#8250 watches Claude Code's guardrail paths with auditd, so a record that leaves the host
is the evidence. The pipeline can be wrong three ways: it tails a path auditd does not write;
its drops catch a keyed watch's records; or it ships the kernel's BPF, netfilter and service
bookkeeping, about 15000 of 27000 lines in 10 hours on daniel-box.

The selectors are read out of the rendered config. Python's `re` stands in for RE2, which is
equivalent for these patterns.
"""

import re

import pytest

from _helpers import ALL_VARS, load_yaml
from _k8s_render import rendered_docs

JOB = load_yaml(ALL_VARS)["loki_streams"]["host_audit"]["job"]
GS = "\x1d"  # auditd's ENRICHED format appends the translated fields after this byte.

# Real line shapes from daniel-box's /var/log/audit/audit.log, 2026-11-13, trimmed.
KEPT = (
    'type=SYSCALL msg=audit(1794549517.822:77687): arch=c000003e syscall=84 success=no exit=-2 items=1 ppid=1681085 pid=1681228 auid=4294967295 uid=0 comm="dpkg" exe="/usr/bin/dpkg" subj=unconfined key="actions"'
    + GS
    + 'ARCH=x86_64 SYSCALL=rmdir AUID="unset"',
    'type=SYSCALL msg=audit(1794549600.101:77701): arch=c000003e syscall=257 success=yes exit=3 items=2 ppid=1700 pid=1701 auid=1000 uid=1000 comm="bash" exe="/usr/bin/bash" subj=unconfined key="claude-memory"'
    + GS
    + 'ARCH=x86_64 SYSCALL=openat AUID="ubuntu"',
    'type=PATH msg=audit(1794549517.822:77687): item=0 name="/etc/sudoers.d/" inode=2097244 dev=fc:00 mode=040755 ouid=0 ogid=0 nametype=PARENT'
    + GS
    + 'OUID="root" OGID="root"',
)
DROPPED = (
    "type=BPF msg=audit(1794549300.517:77604): prog-id=14762 op=UNLOAD",
    "type=SERVICE_START msg=audit(1794549301.071:77619): pid=1 uid=0 auid=4294967295 msg='unit=homelab-sync-artifacts comm=\"systemd\" res=success'"
    + GS
    + 'UID="root"',
    'type=SYSCALL msg=audit(1794549300.517:77604): arch=c000003e syscall=3 success=yes exit=0 items=0 ppid=1663588 pid=1663600 auid=4294967295 uid=0 comm="runc" exe="/usr/bin/runc" subj=unconfined key=(null)'
    + GS
    + "ARCH=x86_64 SYSCALL=close",
)


@pytest.fixture(scope="module")
def alloy_config() -> str:
    for role, _tpl, doc in rendered_docs():
        if role == "loki-homelab" and doc.get("kind") == "ConfigMap":
            return doc["data"]["config.alloy"]
    raise AssertionError(
        "loki-homelab renders no ConfigMap carrying a config.alloy key"
    )


@pytest.fixture(scope="module")
def drop_patterns(alloy_config: str) -> list[str]:
    """The line regex of each drop stage in the `host_audit` block, in order."""
    m = re.search(r'loki\.process "host_audit" \{(.*?)\n\}', alloy_config, re.DOTALL)
    assert m, 'no loki.process "host_audit" block in the rendered Alloy config'
    patterns = []
    for kind, body in re.findall(r"stage\.(\w+)\s*\{(.*?)\n  \}", m[1], re.DOTALL):
        assert kind == "match", f"unexpected stage.{kind} in the host_audit block"
        assert re.search(r'action\s*=\s*"drop"', body), body
        selector = re.search(r'selector\s*=\s*"((?:[^"\\]|\\.)*)"', body)
        assert selector, body
        decoded = re.sub(r"\\(.)", r"\1", selector[1])
        line = re.fullmatch(rf'\{{job="{JOB}"\}} \|~ "([^"\\`]*)"', decoded)
        assert line, f"unexpected selector shape: {decoded}"
        patterns.append(line[1])
    assert patterns, "the host_audit block has no drop stage"
    return patterns


@pytest.mark.parametrize("line", KEPT)
def test_a_keyed_watch_record_ships(drop_patterns: list[str], line: str) -> None:
    assert not any(re.search(p, line) for p in drop_patterns)


@pytest.mark.parametrize("line", DROPPED)
def test_the_unkeyed_bookkeeping_is_dropped(
    drop_patterns: list[str], line: str
) -> None:
    assert any(re.search(p, line) for p in drop_patterns)


def test_the_source_tails_auditds_log_into_this_block(alloy_config: str) -> None:
    m = re.search(
        r'local\.file_match "host_audit" \{\s*path_targets = \[\{"__path__" = "([^"]+)", "job" = "([^"]+)"',
        alloy_config,
    )
    assert m, 'no local.file_match "host_audit" with a path and a job'
    assert (m[1], m[2]) == ("/var/log/audit/audit.log", JOB)
    src = re.search(
        r'loki\.source\.file "host_audit" \{[^}]*forward_to\s*=\s*\[([^\]]*)\]',
        alloy_config,
    )
    assert src and src[1].strip() == "loki.process.host_audit.receiver"
