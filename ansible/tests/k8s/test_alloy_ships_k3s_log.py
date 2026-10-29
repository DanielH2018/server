"""The Alloy `k3s` pipeline ships k3s.log's warn-and-worse lines and nothing below (#6177).

k3s writes its output to a file because journald keeps none of it (setup/k3s's
tasks/unit-logging.yml), and the file keeps four rotations. The pipeline has three ways to be
wrong:

- it tails a path k3s does not write, and the stream stays empty, which reads as a quiet day;
- the keep-filter is inverted or too narrow, and the crash reason is dropped;
- the filter lets info lines through, and the volume the unit-logging comment guards against
  reaches Loki.

The selector and the drop expression are READ OUT of the rendered config rather than restated.
Alloy compiles them as RE2. Python's `re` stands in, which is equivalent for these patterns:
anchors, bracket classes, bounded repeats and alternation, with no backslash escapes.
"""

import re

import pytest

from _helpers import ALL_VARS, load_yaml
from _k8s_render import rendered_docs
from lib import yaml_fast
from lib.repo_paths import K3S_DEFAULTS

# Real line shapes from daniel-box's /var/log/k3s.log.1, 2026-10-28, trimmed. The Go runtime
# lines are the shape a crash prints, written by hand because no crash fell in the sample.
KEPT = (
    'W1028 00:41:00.791109 3683576 dispatcher.go:225] Failed calling webhook, failing closed mutator.longhorn.io: failed calling webhook "mutator.longhorn.io"',
    'E1028 00:00:07.507158 3683576 cronjob_controllerv2.go:202] "Unhandled Error" err="error syncing CronJobController homelab/configarr-health"',
    "F1029 00:00:00.000000    1234 server.go:1] fatal klog",
    'time="2026-10-28T00:43:04Z" level=error msg="Sending HTTP/1.1 502 response to 127.0.0.1:60266: dial tcp 10.42.1.177:9502: connect: connection timed out"',
    'time="2026-10-28T00:58:47Z" level=warning msg="Proxy error: read failed: read tcp 10.42.0.1:54834->10.42.0.239:9443: read: connection reset by peer"',
    'time="2026-10-29T00:00:00Z" level=fatal msg="starting kubernetes: preparing server: init cluster datastore"',
    '{"level":"warn","ts":"2026-10-28T00:54:11.827260Z","logger":"etcd-client","caller":"v3@v3.7.1-k3s3/retry_interceptor.go:68","msg":"retrying of unary invoker failed"}',
    "panic: runtime error: invalid memory address or nil pointer dereference",
    "fatal error: concurrent map writes",
)
DROPPED = (
    'I1028 00:00:00.683020 3683576 server.go:177] "Pod update broadcasted" podUID="308dfbe1-a235-47f9-9558-70a183e1a0b0" type="MODIFIED"',
    'time="2026-10-28T00:00:03Z" level=info msg="Saving etcd snapshot to /var/lib/rancher/k3s/server/db/snapshots/etcd-snapshot-daniel-box-1793145603"',
    '{"level":"info","ts":"2026-10-28T00:00:03.434314Z","logger":"k3s-etcd-client","caller":"snapshot/v3_snapshot.go:83","msg":"created temporary db file"}',
    # kube-router, once per traefik IPv6 ipBlock per policy sync: 29400 of 29688 klog warnings.
    "W1028 00:00:00.795692 3683576 policy.go:622] Ignoring IPv6 source IP blocks [[2400:cb00::/32 timeout 0]] from policy traefik because we are not IPv6 Enabled!",
    # A continuation line belongs to the header before it, and the header is what is kept.
    '\trpc error: code = Unknown desc = failed to pull and unpack image "ghcr.io/danielh2018/ical-proxy:sha-472d7f16182f"',
)

_K3S_BLOCK = re.compile(r'loki\.process "k3s" \{(.*?)\n\}', re.DOTALL)
_RIVER_STRING = r'"((?:[^"\\]|\\.)*)"'


def _river_attr(body: str, name: str) -> str:
    """The decoded value of a stage's River string attribute `name`.

    Only the escapes these expressions use are decoded, `\\"` and `\\\\`.
    """
    m = re.search(rf"{name}\s*=\s*{_RIVER_STRING}", body)
    assert m, f"no {name} attribute in the stage: {body}"
    return re.sub(r"\\(.)", r"\1", m[1])


@pytest.fixture(scope="module")
def alloy_config() -> str:
    for role, _tpl, doc in rendered_docs():
        if role == "loki-homelab" and doc.get("kind") == "ConfigMap":
            return doc["data"]["config.alloy"]
    raise AssertionError(
        "loki-homelab renders no ConfigMap carrying a config.alloy key"
    )


@pytest.fixture(scope="module")
def k3s_block(alloy_config: str) -> str:
    m = _K3S_BLOCK.search(alloy_config)
    assert m, 'no loki.process "k3s" block in the rendered Alloy config'
    return m.group(1)


def _ships(k3s_block: str, line: str) -> bool:
    """Whether a `{job="k3s"}` line survives the block's stages, applied in order."""
    for stage in re.finditer(r"stage\.(\w+)\s*\{(.*?)\n  \}", k3s_block, re.DOTALL):
        kind, body = stage.groups()
        if kind == "match":
            selector = _river_attr(body, "selector")
            assert re.search(r'action\s*=\s*"drop"', body), body
            m = re.fullmatch(r'\{job="k3s"\} (!~|\|~) `([^`]*)`', selector)
            assert m, f"unexpected selector shape: {selector}"
            hit = re.search(m[2], line) is not None
            if hit == (m[1] == "|~"):
                return False
        elif kind == "drop":
            expression = _river_attr(body, "expression")
            if re.search(expression, line):
                return False
        else:
            raise AssertionError(f"unexpected stage.{kind} in the k3s block")
    return True


@pytest.mark.parametrize("line", KEPT)
def test_a_warn_or_worse_line_ships(k3s_block: str, line: str) -> None:
    assert _ships(k3s_block, line)


@pytest.mark.parametrize("line", DROPPED)
def test_an_info_line_or_the_kube_router_ipv6_warning_is_dropped(
    k3s_block: str, line: str
) -> None:
    assert not _ships(k3s_block, line)


def test_the_source_tails_the_file_k3s_writes_into_this_block(
    alloy_config: str,
) -> None:
    m = re.search(
        r'local\.file_match "k3s" \{\s*path_targets = \[\{"__path__" = "([^"]+)", "job" = "([^"]+)"',
        alloy_config,
    )
    assert m, 'no local.file_match "k3s" with a path and a job'
    assert m[1] == yaml_fast.safe_load(K3S_DEFAULTS.read_text())["k3s_log_file"]
    assert m[2] == load_yaml(ALL_VARS)["loki_streams"]["host_k3s"]["job"]
    src = re.search(
        r'loki\.source\.file "k3s" \{[^}]*forward_to\s*=\s*\[([^\]]*)\]', alloy_config
    )
    assert src, 'no loki.source.file "k3s" with a forward_to'
    assert src[1].strip() == "loki.process.k3s.receiver"
